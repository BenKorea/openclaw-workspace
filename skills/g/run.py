#!/usr/bin/env python3
"""g — Gmail/gws-assistant 자연어 피드백 entry point.

사용자 입력(한국어/영어 혼용 가능)을 파싱해 gws-assistant 명령으로 변환·실행.

처리 단계:
    Tier 1: 정규식 fast-path (즉응, LLM 호출 없음).
    Tier 2: Haiku 4.5 의도 파싱 (~1초).
    Tier 3: 모호 시 1턴 확인 메시지 (옵션 A — 비가역 동작은 confidence high 외엔 확인).

Usage:
    python3 run.py                    # 도움말
    python3 run.py <한국어 텍스트>      # 파싱·실행
    python3 run.py 맞아               # = /gws-assistant approve
    python3 run.py 진행 <id> [메모]    # = correct <id> proceed [memo]

Output:
    stdout: gws-assistant 결과 또는 확인 메시지. 비어있으면 침묵.
    exit 0: 정상.
    exit non-zero: 파싱 실패 또는 gws-assistant 실패.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

GWS_RUN = pathlib.Path.home() / ".openclaw/workspace/skills/gws-assistant/run.py"
STATE_PATH = pathlib.Path.home() / ".openclaw/agents/main/memory/gws-assistant.json"
DEFERRED_PATH = pathlib.Path.home() / ".openclaw/agents/main/memory/g_deferred.json"
DEFERRED_TTL_SEC = 60


def _load_state() -> dict | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_deferred(action: str, args: list[str]) -> None:
    """Tier 2 confirmation 프롬프트 출력 시 action+args 를 TTL 60초로 저장.
    다음 yes_context 입력에서 deferred 를 우선 실행 — '/g 맞아' 가 직전 의도를
    승계하지 못하던 휘발성 버그 수정."""
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=DEFERRED_TTL_SEC)
    data = {"action": action, "args": list(args), "expires_at": expires.isoformat()}
    try:
        DEFERRED_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFERRED_PATH.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _consume_deferred() -> dict | None:
    """저장된 deferred action 이 있고 만료 안 됐으면 반환 + 파일 삭제. 만료/없음이면 None."""
    if not DEFERRED_PATH.exists():
        return None
    try:
        data = json.loads(DEFERRED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _clear_deferred()
        return None
    try:
        expires = dt.datetime.fromisoformat(data["expires_at"])
    except (KeyError, ValueError, TypeError):
        _clear_deferred()
        return None
    if dt.datetime.now(dt.timezone.utc) >= expires:
        _clear_deferred()
        return None
    _clear_deferred()
    return data


def _clear_deferred() -> None:
    try:
        DEFERRED_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _get_pending_review_item() -> dict | None:
    """현재 검토 대기 중인 proceed 항목 (bot 이 마지막으로 propose 한 것).
    /g 진행/보류/불필요 (id 없이) 가 자동으로 이 항목에 적용됨."""
    state = _load_state()
    if not state:
        return None
    plan = state.get("pending_plan") or {}
    for it in plan.get("items", []):
        if it.get("category") == "proceed" and it.get("confirm_status") == "pending_review":
            return it
    return None


def _is_id_in_current_plan(thread_id: str) -> bool:
    """주어진 id 가 현재 plan 안에 있는지. /g 진행 <id> 의 분기 결정에 사용."""
    state = _load_state()
    if not state:
        return False
    plan = state.get("pending_plan") or {}
    for it in plan.get("items", []):
        if it.get("msg_id") == thread_id:
            return True
    return False


# 일괄 재분류 위치 토큰 (예: "진행1/불필요", "보류2/진행", "noise3/proceed").
_BULK_TOKEN = re.compile(
    r"^(진행|보류|불필요|proceed|pending|noise)(\d+)/(진행|보류|불필요|proceed|pending|noise)$",
    re.IGNORECASE,
)
_KO_CAT_TO_EN = {
    "진행": "proceed", "보류": "pending", "불필요": "noise",
    "proceed": "proceed", "pending": "pending", "noise": "noise",
}
_EN_CAT_TO_KO = {"proceed": "진행", "pending": "보류", "noise": "불필요"}


def _resolve_bulk_reclassify_tokens(tokens: list[str]) -> tuple[list[str], list[str]]:
    """위치 기반 토큰을 plan 에서 msg_id 로 해석.
    Returns (gws_argv, errors). gws_argv 는 '<thread_id>:<new_cat>' pair list."""
    state = _load_state()
    if not state or not state.get("pending_plan"):
        return ([], ["활성 plan 이 없습니다."])
    plan = state["pending_plan"]
    by_cat: dict[str, list] = {"proceed": [], "pending": [], "noise": []}
    for it in plan.get("items", []):
        cat = it.get("category")
        if cat in by_cat:
            by_cat[cat].append(it)

    pairs: list[str] = []
    errors: list[str] = []
    for tok in tokens:
        m = _BULK_TOKEN.match(tok)
        if not m:
            errors.append(f"형식 오류: '{tok}' (예: 진행1/불필요)")
            continue
        cur_label, idx_str, new_label = m.group(1), m.group(2), m.group(3)
        cur_en = _KO_CAT_TO_EN[cur_label.lower()]
        new_en = _KO_CAT_TO_EN[new_label.lower()]
        idx = int(idx_str)
        bucket = by_cat[cur_en]
        if idx < 1 or idx > len(bucket):
            ko = _EN_CAT_TO_KO[cur_en]
            errors.append(f"'{tok}': {ko} 항목 {len(bucket)}건 — {idx}번 없음")
            continue
        msg_id = bucket[idx - 1].get("msg_id", "")
        if not msg_id:
            errors.append(f"'{tok}': msg_id 누락")
            continue
        if cur_en == new_en:
            continue  # no-op skip
        pairs.append(f"{msg_id}:{new_en}")
    return (pairs, errors)

# ============================================================================
# Tier 1 — 정규식 fast-path
# ============================================================================

_HEX_ID = r"[0-9a-fA-F]{8,}"

PATTERNS_FAST = [
    # ("action_type", regex)
    # 컨텍스트 인식 긍정 — 검토 대기 있으면 confirm_current(즉시 종결), 없으면 approve.
    # "확정/ok/예/네/맞아/좋아/승인/approve/confirm" 모두 동일 의미.
    ("yes_context",     re.compile(r"^(?:확정|confirm|예|네|맞아|좋아|OK|승인|approve)\s*$", re.IGNORECASE)),
    ("skip_current",    re.compile(r"^(?:보류|스킵|skip|나중에)\s*$", re.IGNORECASE)),
    ("dismiss_current", re.compile(r"^(?:불필요|폐기|dismiss)\s*$", re.IGNORECASE)),
    # 답장 — vault+Gmail 검색 → Drafts → awaiting_reply 큐. 발송 자동 감지로 종결.
    ("reply",           re.compile(r"^(?:답장|reply)(?:\s+(.+))?$", re.IGNORECASE)),
    # 답장할일 — 답장 + Google Tasks 등록 (마감일 인자 또는 LLM 추출 또는 None).
    ("reply_task",      re.compile(r"^(?:답장할일|reply[-_\s]?task)(?:\s+(.+))?$", re.IGNORECASE)),
    # 할일 — Google Tasks 등록만 + 즉시 종결. 인자: [YYYY-MM-DD] [note...] (둘 다 선택).
    ("task",            re.compile(r"^(?:할일|task)(?:\s+(.+))?$", re.IGNORECASE)),
    # 경로수정 — PARA 폴더 변경 + 즉시 종결 (재확인 없음).
    ("relocate",        re.compile(r"^(?:경로\s*수정|경로수정|relocate)(?:\s+(.+))?$", re.IGNORECASE)),
    # id 있음 — 다른 메일 후처리 교정 (correct/reclassify, 컨텍스트 자동 분기)
    ("correct_proceed", re.compile(rf"^(?:진행|proceed)\s+({_HEX_ID})\b\s*(.*)$", re.IGNORECASE)),
    ("correct_pending", re.compile(rf"^(?:보류|pending)\s+({_HEX_ID})\b\s*(.*)$", re.IGNORECASE)),
    ("correct_noise",   re.compile(rf"^(?:불필요|noise)\s+({_HEX_ID})\b\s*(.*)$", re.IGNORECASE)),
    # 기타
    ("cancel",          re.compile(r"^(?:취소|cancel)\s*$", re.IGNORECASE)),
    ("status",          re.compile(r"^(?:상태|status|확인)\s*$", re.IGNORECASE)),
    ("force_poll",      re.compile(r"^(?:다시\s*분류|재분류|폴링|force\s*poll|새로\s*받기)\s*$", re.IGNORECASE)),
    # 보류 박스 정리 — 'brainify/pending' 라벨 메일을 batch_size 건 가져와 재분류 plan 으로 변환
    ("pending_review",  re.compile(r"^(?:보류\s*정리|보류정리|pending[-\s_]?review)(?:\s+(\d+))?\s*$", re.IGNORECASE)),
    # 일괄 재분류 — '재분류 진행1/불필요 보류2/진행 …' 위치 기반. tier1_parse 에서 토큰 split 처리.
    ("bulk_reclassify", re.compile(r"^(?:재분류|reclassify|bulk[-_\s]*reclassify)\s+(.+)$", re.IGNORECASE)),
    ("learn_rules",     re.compile(r"^(?:학습|규칙\s*학습|learn[-_\s]?rules)\s*$", re.IGNORECASE)),
    ("show_rules",      re.compile(r"^(?:규칙|규칙\s*보여줘|show[-_\s]?rules)\s*$", re.IGNORECASE)),
]


_TRAILING_PUNCT = "'\"`.,!?。、"


def tier1_parse(text: str) -> tuple[str, list[str]] | None:
    """Returns (action_type, args) or None.
    입력 끝의 punctuation/quote 를 제거 후 매칭 — '/g 취소'' / '/g 취소.' 등이
    Tier 2 LLM 으로 떨어지지 않고 즉시 분기되도록."""
    text = text.strip().rstrip(_TRAILING_PUNCT)
    for action_type, pat in PATTERNS_FAST:
        m = pat.match(text)
        if not m:
            continue
        if action_type.startswith("correct_"):
            cat = action_type.split("_", 1)[1]
            tid = m.group(1)
            memo = (m.group(2) or "").strip() if m.lastindex and m.lastindex >= 2 else ""
            args = [tid, cat]
            if memo:
                args.append(memo)
            return ("correct", args)
        if action_type == "reply":
            raw = (m.group(1) or "").strip()
            return ("reply", raw.split() if raw else [])
        if action_type == "reply_task":
            raw = (m.group(1) or "").strip()
            return ("reply_task", raw.split() if raw else [])
        if action_type == "task":
            raw = (m.group(1) or "").strip()
            return ("task", raw.split() if raw else [])
        if action_type == "relocate":
            raw = (m.group(1) or "").strip()
            return ("relocate", raw.split() if raw else [])
        if action_type == "pending_review":
            n = m.group(1)
            return ("pending_review", [n] if n else [])
        if action_type == "bulk_reclassify":
            raw = m.group(1).strip()
            return ("bulk_reclassify", raw.split())
        return (action_type, [])
    return None


# ============================================================================
# Tier 2 — Haiku 자연어 파싱
# ============================================================================

TIER2_PROMPT_TMPL = """다음 사용자 입력을 Gmail 브리핑 봇 명령으로 매핑하라. **반드시 JSON 객체만 응답하라** (다른 텍스트 금지).

지원되는 action (대부분은 bot 의 propose 1건 검토 항목 대상; thread_id 불요 — 단일 pending_review 자동 보강):
- confirm_current: 즉시 종결 ("확정/ok/예/네/맞아"). 노트 PARA 이동 + '브레인화/완료' + archive.
- skip_current: 보류 처리 ("보류/스킵/나중에"). 노트 삭제 + '브레인화/보류' (inbox 유지).
- dismiss_current: 불필요 폐기 ("불필요/폐기"). 노트 삭제 + '브레인화/불필요' + archive.
- reply: 답장 초안 등록 ("답장해줘/회신 작성"). args = ["<지시 한 문장>"] (선택). vault+Gmail 검색 → Drafts. 발송 자동 감지 시 종결.
- reply_task: 답장 + Google Tasks 합성 ("답장하고 할일도/마감 있는 답장"). args = [<YYYY-MM-DD 선택>, "<지시 선택>"]. 마감일은 본문에 명시되어 있으면 절대 날짜로 추출.
- task: Google Tasks 등록만 ("할일로 등록/마감 추가"). args = [<YYYY-MM-DD 선택>]. 즉시 종결.
- relocate: PARA 폴더 변경 ("폴더 바꿔/경로 수정"). args = ["folder=<경로>"]. 즉시 종결, 재확인 없음.
- approve: 현재 plan 의 noise/pending 일괄 라벨 + proceed 1건씩 검토 시작
- cancel: 현재 plan 폐기
- status: 현재 state/plan 상태 출력
- force_poll: 새 분류 강제 실행
- correct: 다른 (검토 외) 메일 분류 변경. args = [thread_id, "proceed|pending|noise", "메모(선택)"]
- learn_rules / show_rules: deterministic 규칙 학습/표시
- noop: 인사·감사 등 동작 없음
- unknown: 의도 불명

규칙:
- thread_id 는 보통 16자리 hex 문자열.
- thread_id 명시 안 됨 + 검토 대기 항목 행위면 confirm/skip/dismiss/reply/reply_task/task/relocate_current 매핑.
- thread_id 명시되면 correct 매핑 (다른 메일 후처리).
- 사용자가 여러 thread 동시 지목은 unknown.
- confidence: high (입력이 명확) | med (합리적 추정) | low (자신없음).
- need_confirm: 비가역 동작(approve/cancel/correct/reply/reply_task/task/relocate) + confidence != high 면 true. status/force_poll/noop 은 항상 false.
- summary: 사용자 확인용 한국어 한 줄.

응답 형식:
{{"action":"confirm_current|skip_current|dismiss_current|reply|reply_task|task|relocate|approve|cancel|status|force_poll|correct|learn_rules|show_rules|noop|unknown","args":[...],"confidence":"high|med|low","need_confirm":true|false,"summary":"..."}}

사용자 입력:
{text}
"""


def tier2_parse_with_llm(text: str) -> dict | None:
    prompt = TIER2_PROMPT_TMPL.format(text=text)
    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-haiku-4-5",
        "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if "```" in out:
        try:
            inner = out.split("```", 2)[1]
            if inner.startswith("json"):
                inner = inner[4:]
            out = inner.strip()
            if out.endswith("```"):
                out = out[:-3].strip()
        except IndexError:
            pass
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        try:
            start = out.index("{")
            end = out.rindex("}") + 1
            return json.loads(out[start:end])
        except (ValueError, json.JSONDecodeError):
            return None


# ============================================================================
# gws-assistant 호출
# ============================================================================

def run_gws(*args: str) -> int:
    """gws-assistant run.py 호출. stdout 그대로 출력.
    status 등 stderr 로 출력하는 명령은 stderr 도 stdout 으로 merge (사용자 가시성 보장)."""
    cmd = ["python3", str(GWS_RUN), *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    merge_stderr_to_stdout = (args and args[0] in ("status",))
    out = r.stdout or ""
    if merge_stderr_to_stdout and r.stderr:
        out = (out.rstrip() + "\n" + r.stderr).strip() + "\n" if out else r.stderr
    if out:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")
    if r.returncode != 0 and r.stderr and not merge_stderr_to_stdout:
        sys.stderr.write(r.stderr)
    return r.returncode


def _no_pending_review_msg() -> int:
    print("[g] 현재 검토 대기 중인 항목이 없습니다.")
    print("    bot 의 /g 맞아 (= approve) 후 propose 가 떠야 진행/보류/불필요 (id 없이) 가 동작합니다.")
    print("    또는 다른 메일을 후처리 교정하려면 id 를 같이: /g 진행 <thread_id>")
    return 1


def execute(action: str, args: list[str]) -> int:
    if action == "yes_context":
        # 1) Tier 2 deferred action 우선 — 직전 confirmation 프롬프트의 의도 승계.
        deferred = _consume_deferred()
        if deferred:
            print(f"[g] deferred action 실행: {deferred.get('action')} {deferred.get('args') or ''}".rstrip())
            return execute(deferred["action"], deferred.get("args") or [])
        # 2) 일반 분기: 검토 대기 항목 있으면 confirm, 없으면 approve.
        item = _get_pending_review_item()
        if item:
            return run_gws("confirm", item["msg_id"])
        return run_gws("approve")
    if action == "approve":
        return run_gws("approve")
    if action == "cancel":
        return run_gws("cancel")
    if action == "status":
        return run_gws("status")
    if action == "force_poll":
        return run_gws("--force-poll")
    if action == "correct":
        if not args or len(args) < 2:
            print("[g] correct 에는 thread_id 와 카테고리가 필요합니다.")
            return 1
        # 컨텍스트 인식: id 가 현재 plan 안이면 in-plan reclassify (Gmail 라벨 안 건드림),
        # 밖이면 후처리 correct (Gmail 라벨 변경).
        tid = args[0]
        if _is_id_in_current_plan(tid):
            return run_gws("reclassify", *args)
        return run_gws("correct", *args)
    if action == "confirm_current":
        item = _get_pending_review_item()
        if not item:
            return _no_pending_review_msg()
        return run_gws("confirm", item["msg_id"])
    if action == "skip_current":
        # 컨텍스트 자동 인식:
        #  - 개별 review 대기 항목 있음 → cmd_skip (이 항목만 보류)
        #  - review 대기는 없지만 plan 이 있음 → cmd_snooze 60 (plan 전체 60분 보류)
        #  - 아무것도 없음 → 친절 안내
        item = _get_pending_review_item()
        if item:
            return run_gws("skip", item["msg_id"])
        state = _load_state()
        if state and state.get("pending_plan"):
            return run_gws("snooze", "60")
        return _no_pending_review_msg()
    if action == "dismiss_current":
        item = _get_pending_review_item()
        if not item:
            return _no_pending_review_msg()
        return run_gws("dismiss", item["msg_id"])
    if action == "reply":
        return run_gws("reply", *args)
    if action == "reply_task":
        return run_gws("reply-task", *args)
    if action == "task":
        return run_gws("gtask", *args)
    if action == "relocate":
        if not args:
            print("[g] 경로수정 에는 folder=<경로> 가 필요합니다.")
            return 1
        return run_gws("edit", *args)
    if action == "learn_rules":
        return run_gws("learn-rules")
    if action == "show_rules":
        return run_gws("show-rules")
    if action == "pending_review":
        # batch_size 옵션 인자 (기본 5)
        if args:
            return run_gws("pending-review", args[0])
        return run_gws("pending-review")
    if action == "bulk_reclassify":
        if not args:
            print("[g] 재분류 항목 토큰이 필요합니다 (예: '진행1/불필요').")
            return 1
        pairs, errors = _resolve_bulk_reclassify_tokens(args)
        for e in errors:
            print(f"[g] {e}")
        if not pairs:
            print("[g] 적용할 재분류가 없습니다.")
            return 1 if errors else 0
        return run_gws("bulk-reclassify", *pairs)
    if action == "noop":
        # 침묵
        return 0
    print(f"[g] 지원하지 않는 action: {action}")
    return 1


# ============================================================================
# main
# ============================================================================

HELP_TEXT = """[g] Gmail/gws-assistant 자연어 entry point.

사용법: /g <한국어 텍스트>

bot 의 propose (1건 검토 요청) 에 답할 때 (모두 즉시 종결 또는 자동 종결):
  /g 확정 (= ok)                       — 노트 PARA 이동 + 라벨 '브레인화/완료'
  /g 답장 (= reply)                    — vault+Gmail 검색 → Drafts → awaiting_reply 큐.
                                         발송 자동 감지 시 '브레인화/완료' 로 promote.
  /g 답장할일 [YYYY-MM-DD]             — 답장 + Google Tasks 등록 (마감일 인자/LLM 추출/없음)
  /g 할일 [YYYY-MM-DD] [note] (= task) — Google Tasks 등록만 + 즉시 종결
  /g 경로수정 folder=<경로>            — PARA 폴더 변경 + 즉시 종결 (재확인 없음)
  /g 보류                              — 노트 삭제 + 보류 라벨 (inbox 유지) / plan 단위 시 60분 snooze
  /g 불필요                            — 노트 삭제 + 불필요 + archive

(컨텍스트 자동 인식 — 개별 review 대기 있으면 그 항목 적용, 없으면 plan 단위 동작.
 표시되지 않은 한국어/영어 alias 도 파서는 동일하게 인식 — 예/네/맞아/skip/dismiss 등.)

id 와 함께 (다른 메일 후처리 교정):
  /g 진행 19c55ca2baf47bdb         — plan 안: category 변경 / plan 밖: 라벨 교정
  /g 보류 19c55ca2baf47bdb 학회공문 — 메모와 함께
  /g 불필요 19c55ca2baf47bdb

기타 빠른 명령:
  /g 맞아                          — 현재 plan 일괄 처리 (approve)
  /g 취소                          — plan 폐기
  /g 상태                          — 현재 plan/state (awaiting_reply 큐 포함)
  /g 다시 분류                     — 강제 폴링
  /g 보류 정리 [N]                 — '브레인화/보류' 라벨 메일 N건(기본 5) 라벨 제거 후 재분류 plan
  /g 재분류 진행1/불필요 보류2/진행 …  — plan 내 위치 기반 일괄 재분류 (Gmail 라벨은 안 건드림)
  /g 학습                          — 교정 로그 → deterministic 규칙 추출
  /g 규칙                          — 활성 규칙 표시

자연어 (Tier 2 — Haiku 파싱, 비가역 동작은 1턴 확인):
  /g 두 번째는 보류로 바꿔줘
  /g 정중하게 거절 답장
  /g 5월 15일까지 답장하고 할일도
"""


def main(argv: list[str]) -> int:
    if not argv:
        print(HELP_TEXT.rstrip())
        return 0

    text = " ".join(argv).strip()
    if not text:
        print(HELP_TEXT.rstrip())
        return 0

    # Tier 1: 정규식 fast-path
    fast = tier1_parse(text)
    if fast:
        action, args = fast
        return execute(action, args)

    # Tier 2: LLM 파싱
    parsed = tier2_parse_with_llm(text)
    if not parsed:
        print(f"[g] 입력을 이해하지 못했습니다: '{text}'")
        print("도움말은 인자 없이 /g 호출.")
        return 1

    action = parsed.get("action", "unknown")
    args = parsed.get("args") or []
    confidence = (parsed.get("confidence") or "low").lower()
    need_confirm = bool(parsed.get("need_confirm"))
    summary = (parsed.get("summary") or "").strip()

    if action == "unknown":
        print(f"[g] 입력을 이해하지 못했습니다: '{text}'")
        if summary:
            print(f"  Haiku 해석: {summary}")
        return 1

    if action == "noop":
        # 침묵 (인사 등)
        return 0

    # 옵션 A: 비가역 + confidence 낮으면 1턴 확인
    if need_confirm or confidence == "low":
        msg_lines = ["[g] 다음 동작을 실행할까요? (확인 필요)"]
        if summary:
            msg_lines.append(f"  → {summary}")
        msg_lines.append(f"  → action={action}, args={args}, confidence={confidence}")
        msg_lines.append(f"  실행하려면: /g 맞아  (TTL {DEFERRED_TTL_SEC}초 — 그 안에 입력해야 이 의도가 승계됨, 아니면 다시 입력)")
        # deferred state 저장 — 다음 yes_context 입력에서 _consume_deferred 로 회수.
        _save_deferred(action, args)
        print("\n".join(msg_lines))
        return 0

    return execute(action, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
