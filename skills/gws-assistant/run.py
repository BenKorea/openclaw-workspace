#!/usr/bin/env python3
"""gws-assistant — Gmail 브리핑 + 브레인화 plan + Telegram 양방향 승인.

운영 모델 (Phase 1, 3분류 확정 2026-05-08):
    - cron 이 평일 10분 간격으로 본 스크립트를 폴링
    - 게이트(평일/근무시간/공휴일/미팅 중-`판독`예외) 통과 + plan 변경 시 발화
    - 발화 = 받은편지함 미분류 메일(라벨 없음, 읽음/안읽음 무관) 3분류(진행·보류·불필요) plan + Telegram 메시지 + 승인 명령 안내
    - 사용자는 Telegram 에서 /gws-assistant approve|cancel|snooze N 으로 응답
    - approve: pending plan 의 자동 처리 항목 실행 (모든 항목 라벨/archive)
    - cancel: pending plan 폐기 → 다음 폴링에서 재분류
    - snooze N: N 분 동안 발화 보류
    - pending plan 은 명시 폐기/승인 까지 살아있음 (자동 만료 없음)
    - 미응답 plan + 신규 메일은 다음 폴링에 머지되어 한 메시지로 발화

라벨 정책:
    라벨은 "콘텐츠 카테고리" 가 아니라 "브레인화 작업 진행 상태" 표시.
    - 브레인화/진행 — 노트 작성됨 + 후속 작업(회신/일정/할일) 진행 중. **임시 라벨**
                    (followups 가 비어있지 않은 confirm 결과)
    - 브레인화/완료 — 후속 작업까지 모두 종결. **영구 보존 (terminal)**
                    (followups 가 비어있는 confirm 자동 진입 또는 `/g 완료 <id>` 수동 promote)
    - 브레인화/보류 — 외부 액션 대기 또는 분류 자신없음 (라벨, inbox 유지)
    - 브레인화/불필요 — 광고·자동알림·중복 등 브레인화 대상 아님 (라벨 + archive)
    Legacy `브레인화/중복` 은 그대로 두고 inbox 검색에서만 제외.

Usage:
    python3 run.py                  # cron 폴 (게이트 + 머지 + 발화)
    python3 run.py --force-poll     # 게이트 무시하고 강제 폴 (테스트용)
    python3 run.py approve          # pending plan 실행
    python3 run.py cancel           # pending plan 폐기
    python3 run.py snooze N         # N분 발화 보류
    python3 run.py status           # 현 상태 한 화면 (디버그, stderr)

Output:
    stdout: Telegram 으로 보낼 메시지. 비어있으면 침묵.
    exit 0: 정상.
    exit non-zero: 인프라 실패 (gog OAuth 등).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

# ============================================================================
# Configuration
# ============================================================================

ACCOUNT = "kimbi.kirams@gmail.com"
KST = dt.timezone(dt.timedelta(hours=9), name="KST")

STATE_PATH = pathlib.Path.home() / ".openclaw/agents/main/memory/gws-assistant.json"
DETERMINISTIC_RULES_PATH = pathlib.Path.home() / ".openclaw/agents/main/memory/gws-deterministic.json"
VAULT_ROOT = pathlib.Path.home() / "projects/2nd-brain-vault"
VAULT_CAPTURE_DOC = VAULT_ROOT / "knowledge/02_areas/brain-system/workflows/gmail-capture.md"
VAULT_INBOX = VAULT_ROOT / "sources/00_inbox"
VAULT_ATTACH_ROOT = VAULT_INBOX / "_attachments"

WATCH_CALENDARS = [
    ("kimbi.kirams@gmail.com", "개인"),
    ("b78548050efd9ab3d15ec4365e5681328944398adc6bf00f413799cf81b0df09@group.calendar.google.com", "대한핵의학회"),
    ("ee3d92b9cec1e9546735458911f9f3f6ac8b722f26e73a9c79dd919ec62ab219@group.calendar.google.com", "방어학회"),
    ("gk40qg9l3pr8jogd79g0vksv6453thbf@import.calendar.google.com", "카카오톡"),
]
HOLIDAY_CAL = "ko.south_korea#holiday@group.v.calendar.google.com"

WORK_START = dt.time(8, 30)
WORK_END = dt.time(17, 30)
GMAIL_MAX = 5
NOTIFIED_CAP = 1000

BUSY_EXCEPTIONS = ["판독"]
DOW_KR = ["월", "화", "수", "목", "금", "토", "일"]

# Heuristic classifier — Phase 1 보수적 룰.
# from 주소(소문자) 에 아래 패턴 중 하나라도 substring 매칭 → 자동 [불필요].
# 그 외엔 LLM 분류로 넘어감.
NOISE_FROM_PATTERNS = [
    "noreply", "no-reply", "newsletter", "newslett", "marketing", "promo",
    "notifications-noreply", "messages-noreply", "invitations@linkedin",
    "recommends@", "ship@info.vercel", "googleaistudio-noreply",
    "scholaralerts-noreply",
    # 알려진 광고·뉴스레터 도메인 (조심스럽게 추가)
    "@mail.gostanford.com", "@mail.greatclips.com", "@hello.design.com",
    "@info.vercel.com", "@mc.ihg.com", "@promo.melia.com",
    "@research.springernature.com", "@email.sagepub.com",
    "@mp1.tripadvisor.com", "@mail.grammarly.com",
    "@quora.com", "english-personalized-digest@quora",
]

# ── 8-라벨 액션 트리아지 모델 (gmail-capture.md §11) ──
# Dr. Ben 이 폰/PC 에서 직접 부착하는 액션 라벨. 1~8 전부 완전무인 처리
# (§11.3·§11.5). 2026-05-17 8-라벨 멱집합 완성 + 구 3-라벨 레거시 삭제.
LABEL_SAVE = "1 저장"        # {} 행동 없음 — audit 동반 노트 + archive
LABEL_DONE_9 = "9 완료"      # 터미널 표식 (기계 검증된 완료)
LABEL_SCHEDULE = "2 일정"    # {일정} — audit 노트 + Google Calendar 이벤트 (§11.5)
LABEL_TASK = "6 할일"        # {할일} — audit 노트 + Google Tasks (§11.5)
LABEL_REPLY = "8 회신"       # {회신} — 초안 작성 + 2단계 비-terminal 종결 (§11.5 spec-lock 2026-05-17)
LABEL_SCHED_TASK = "3 일정+할일"  # {일정,할일} — 2·6 순수 terminal 합성 (§11.5)
LABEL_SCHED_REPLY = "4 일정+회신"       # {일정,회신} — 2+8, 2단계 비-terminal (§11.5)
LABEL_SCHED_TASK_REPLY = "5 일정+할일+회신"  # {일정,할일,회신} — 2+6+8 (§11.5)
LABEL_TASK_REPLY = "7 할일+회신"        # {할일,회신} — 6+8, 2단계 비-terminal (§11.5)
GMAIL_SAVE_MAX = 8           # 1회 드레인 상한 (멱등 — 잔여분 다음 사이클)
REPLY_SENT_WINDOW_DAYS = 2   # 회신 브레인화: 보낸편지함 스캔 윈도우 (멱등이 중복 차단)

# ── tick당 무거운 처리 전역 예산 (FailoverError 방어, 2026-05-19) ──
# main 백엔드 = claude-cli → run.py 가 *끝나야* OpenClaw 가 보는 JSONL 이
# 방출된다 (실행 중엔 Bash 도구 캡처라 0개). run.py 가 600s 넘게 침묵하면
# OpenClaw watchdog 이 "CLI produced no output for 600s and was terminated"
# → FailoverError → 폴백 모델 회전. run.py 내부 stdout 하트비트는 이
# watchdog 까지 전파 안 됨(claude-cli 백엔드 스트림이 감시 대상). 따라서
# 유일하게 통하는 run.py 층 방어 = run 자체를 600s 한참 아래로 묶기.
# 신규 1건 ≈ 노트생성(claude --print) 180s (+첨부 N×180s). 9개 드레인
# 핸들러가 이 예산을 *공유* — GMAIL_SAVE_MAX 는 핸들러별이라 9× 합산
# 폭주를 못 막는다. 예산 소진 시 잔여 메일은 라벨/노트존재 가드로 살아
# 다음 30분 tick 이 멱등 재개. 잔여 위험 = 단일 메일 첨부 다수 fan-out
# (별도 과제). 튜닝값.
POLL_HEAVY_BUDGET = 3
# None = 무제한 (수동 서브커맨드·import 기본). cmd_poll(cron 경로)에서만
# _arm_tick_budget() 으로 유한값 장전 → 수동 드레인·dry-run 동작 불변.
_TICK_HEAVY_REMAINING = None


def _arm_tick_budget() -> None:
    """cron poll 진입 시 유한 예산 장전. 미호출 = 무제한 (수동 경로)."""
    global _TICK_HEAVY_REMAINING
    _TICK_HEAVY_REMAINING = POLL_HEAVY_BUDGET


def _tick_budget_left() -> bool:
    return _TICK_HEAVY_REMAINING is None or _TICK_HEAVY_REMAINING > 0


def _spend_tick_budget() -> None:
    """무거운 메일 1건 처리 소비. 무제한 모드면 무효과."""
    global _TICK_HEAVY_REMAINING
    if _TICK_HEAVY_REMAINING is not None:
        _TICK_HEAVY_REMAINING -= 1
# 파서 추상화 provenance (§11.3). provider 교체 시 _parser_* 가 자기 id/version 반환.
PARSER_VERSION = "1"



# ============================================================================
# Time / clock — always KST regardless of system TZ
# ============================================================================

def now_kst() -> dt.datetime:
    return dt.datetime.now(tz=KST)


# ============================================================================
# State I/O — atomic write, defensive load
# ============================================================================

def empty_state() -> dict:
    n = now_kst()
    return {
        "last_checked": n.isoformat(),
        "snooze_until": None,
        "pending_plan": None,
        "awaiting_reply": [],
        "gtasks_list_id": None,
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open() as f:
                state = json.load(f)
            state.setdefault("last_checked", "")
            state.setdefault("snooze_until", None)
            state.setdefault("pending_plan", None)
            state.setdefault("awaiting_reply", [])
            state.setdefault("gtasks_list_id", None)
            # deprecated keys
            for k in ("last_batch_date", "notified_msg_ids", "notified_event_ids"):
                state.pop(k, None)
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return empty_state()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gws-assistant.", dir=str(STATE_PATH.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ============================================================================
# gog CLI wrapper
# ============================================================================

def gog_json(*args: str, timeout: int = 30):
    """`gog ... -j --results-only --account ACCOUNT`. Returns parsed JSON,
    [] on empty output, None on infrastructure failure."""
    cmd = ["gog", *args, "--account", ACCOUNT, "-j", "--results-only"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def gog_call(*args: str, timeout: int = 30) -> tuple[bool, str]:
    """For mutating commands. Returns (ok, stderr_or_stdout)."""
    cmd = ["gog", *args, "--account", ACCOUNT]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return (False, (err or out).strip())
    # gog 가 googleapi 에러를 출력하면서도 exit 0 으로 끝내는 케이스 방어.
    # 예: 'Invalid label' 등 → wrapper 에서 실패로 surface 해야 봇이 [확정 실패] 보고.
    combined = f"{out}\n{err}".strip()
    if "googleapi: Error" in combined:
        return (False, combined)
    return (True, out)


# ============================================================================
# Google Tasks helpers
# ============================================================================

GTASK_LIST_NAMES = ("Brainify", "메일 후속")  # 우선순위 — 기존 list 재사용


def ensure_gtasks_list(state: dict) -> str | None:
    """Tasks list id 보장. state 에 캐시 → 없으면 위 이름들 중 첫 매칭 → 없으면 'Brainify' 신규 생성.
    실패 시 None (gog 무응답 또는 OAuth)."""
    cached = state.get("gtasks_list_id")
    if cached:
        return cached
    lists = gog_json("tasks", "lists", "list")
    if lists is None:
        return None
    if isinstance(lists, dict):
        lists = lists.get("items", [])
    for L in lists or []:
        if L.get("title") in GTASK_LIST_NAMES:
            state["gtasks_list_id"] = L.get("id")
            return state["gtasks_list_id"]
    out = gog_json("tasks", "lists", "create", GTASK_LIST_NAMES[0])
    if isinstance(out, dict) and out.get("id"):
        state["gtasks_list_id"] = out["id"]
        return state["gtasks_list_id"]
    return None


def create_gtask(state: dict, title: str, notes: str,
                 due_date: str | None = None) -> str | None:
    """Google Tasks 에 task 생성. due_date='YYYY-MM-DD' (시간 없음, Google 측에서 시간 무시).
    Returns gtask_id 또는 None (실패)."""
    list_id = ensure_gtasks_list(state)
    if not list_id:
        return None
    args = ["tasks", "add", list_id, "--title", title, "--notes", notes]
    if due_date:
        args.extend(["--due", due_date])
    out = gog_json(*args)
    if isinstance(out, dict) and out.get("id"):
        return out["id"]
    return None


# ============================================================================
# Gates
# ============================================================================


# ============================================================================
# Data fetch
# ============================================================================


# ============================================================================
# Vault guide loader
# ============================================================================


# ============================================================================
# Classifier (heuristic)
# ============================================================================


# ============================================================================
# Plan merge
# ============================================================================


# ============================================================================
# Formatting
# ============================================================================


# ============================================================================
# Brainify helpers — companion note generation for `proceed` items
# ============================================================================

_SLUG_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_SLUG_WS = re.compile(r"\s+")


def _slugify(text: str, max_len: int = 40) -> str:
    """Filename-safe slug. 한글 보존, 위험 문자만 치환, 공백→하이픈."""
    if not text:
        return "untitled"
    s = _SLUG_BAD.sub("", text)
    s = _SLUG_WS.sub("-", s).strip("-._")
    return (s[:max_len] or "untitled").rstrip("-._")


def _decode_b64url(data: str) -> str:
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_parts(payload: dict):
    """Yield (mime_type, decoded_text) for every leaf part."""
    body = (payload or {}).get("body") or {}
    mime = (payload or {}).get("mimeType", "")
    if body.get("data"):
        yield mime, _decode_b64url(body["data"])
    for part in (payload or {}).get("parts", []) or []:
        yield from _walk_parts(part)


def _extract_plain_text(payload: dict) -> str:
    plain, fallback = [], []
    for mime, text in _walk_parts(payload):
        if not text:
            continue
        if mime == "text/plain":
            plain.append(text)
        elif mime.startswith("text/"):
            fallback.append(text)
    return ("\n\n".join(plain) if plain else "\n\n".join(fallback)).strip()


def _headers_to_dict(headers: list) -> dict:
    out = {}
    for h in headers or []:
        name = (h.get("name") or "").lower()
        if name and name not in out:  # keep first occurrence
            out[name] = h.get("value", "")
    return out


def fetch_thread_full(thread_id: str, out_dir: pathlib.Path | None = None):
    """`gog gmail thread get <id> --full` → thread payload dict.
    `--download` 가 같이 붙으면 gog 의 -j 출력이 thread payload 가 아니라
    **첨부 메타 list** 로 바뀌므로, payload fetch 와 attachment download 를 두 호출로 분리한다.
    out_dir 가 주어지면 별도 호출로 첨부 다운로드만 수행 (디스크 사이드이펙트만 사용,
    stdout 의 첨부 list 는 무시 — _extract_attachment_paths 가 fallback 으로 디렉토리 스캔)."""
    payload = gog_json("gmail", "thread", "get", thread_id, "--full", timeout=60)
    if not isinstance(payload, dict):
        return None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        gog_json(
            "gmail", "thread", "get", thread_id, "--full",
            "--download", "--out-dir", str(out_dir),
            timeout=60,
        )
    return payload


def _pick_target_message(thread_payload: dict, target_id: str) -> dict | None:
    """Find the message matching target_id; fall back to first message."""
    thread = thread_payload.get("thread") or {}
    msgs = thread.get("messages") or []
    if not msgs:
        return None
    for m in msgs:
        if m.get("id") == target_id:
            return m
    return msgs[0]


def _extract_attachment_paths(thread_payload: dict, out_dir: pathlib.Path) -> list[pathlib.Path]:
    """Return on-disk paths to attachments from gog `downloaded` field, when present.
    Falls back to scanning out_dir for any non-hidden files (last-resort)."""
    downloaded = thread_payload.get("downloaded")
    paths: list[pathlib.Path] = []
    if isinstance(downloaded, list):
        for entry in downloaded:
            p = entry.get("path") if isinstance(entry, dict) else None
            if p:
                paths.append(pathlib.Path(p))
    elif isinstance(downloaded, dict):
        for entry in downloaded.get("files", []) or []:
            p = entry.get("path") if isinstance(entry, dict) else None
            if p:
                paths.append(pathlib.Path(p))
    if not paths and out_dir.exists():
        for child in out_dir.iterdir():
            if child.is_file() and not child.name.startswith("."):
                paths.append(child)
    return paths


def _parse_internal_date(internal_ms: str | int | None) -> dt.date | None:
    if not internal_ms:
        return None
    try:
        return dt.datetime.fromtimestamp(int(internal_ms) / 1000, tz=KST).date()
    except (ValueError, TypeError, OSError):
        return None


def _vault_frontmatter_guide() -> str:
    """Extract §4 (frontmatter standard) from vault gmail-capture.md."""
    try:
        text = VAULT_CAPTURE_DOC.read_text(encoding="utf-8")
    except OSError:
        return ""
    start = text.find("## 4. 동반 노트 프론트매터 표준")
    if start < 0:
        return ""
    end = text.find("\n## 5. ", start)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def _vault_para_tree(max_depth: int = 2) -> str:
    """vault knowledge/ 디렉토리 트리 (max_depth 깊이) 텍스트 — LLM 의 proposed_para_path 추론용.
    숨김·_ 시작·파일은 제외, 디렉토리만."""
    base = VAULT_ROOT / "knowledge"
    if not base.is_dir():
        return ""
    lines: list[str] = []

    def walk(p: pathlib.Path, depth: int, indent: str) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(p.iterdir())
        except OSError:
            return
        for child in children:
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name.startswith("_"):
                continue
            lines.append(f"{indent}- {child.name}/")
            walk(child, depth + 1, indent + "  ")

    walk(base, 1, "")
    return "\n".join(lines)


def _parse_frontmatter(note_text: str) -> dict:
    """Lightweight YAML frontmatter parser — string scalars + simple lists only.
    Anything more complex (nested dicts, multi-line scalars) is ignored gracefully."""
    if not note_text.startswith("---"):
        return {}
    end = note_text.find("\n---", 3)
    if end < 0:
        return {}
    fm_block = note_text[3:end].lstrip("\n")
    out: dict = {}
    cur_key: str | None = None
    for raw in fm_block.split("\n"):
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("  - "):
            val = line[4:].strip()
            if cur_key is not None:
                if not isinstance(out.get(cur_key), list):
                    out[cur_key] = []
                out[cur_key].append(val)
            continue
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                cur_key = k
                out[k] = []
            elif v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                out[k] = [x.strip() for x in inner.split(",") if x.strip()]
                cur_key = None
            else:
                out[k] = v
                cur_key = None
    return out


def _replace_fm_field(text: str, key: str, value: str) -> str:
    """Replace a single-line scalar field 'key: value' inside frontmatter; append if absent."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    fm_block = text[3:end]
    rest = text[end:]
    new_line = f"{key}: {value}"
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if pat.search(fm_block):
        new_fm = pat.sub(new_line, fm_block)
    else:
        new_fm = fm_block.rstrip() + "\n" + new_line + "\n"
    return "---" + new_fm + rest


def _replace_fm_field_list(text: str, key: str, values: list[str]) -> str:
    """Replace a list field 'key:\\n  - ...' inside frontmatter; append if absent."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end < 0:
        return text
    fm_block = text[3:end]
    rest = text[end:]
    if values:
        new_block = f"{key}:\n" + "\n".join(f"  - {v}" for v in values)
    else:
        new_block = f"{key}: []"
    pat = re.compile(
        rf"^{re.escape(key)}:.*?(?=\n[A-Za-z_][^\s:]*:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    if pat.search(fm_block):
        new_fm = pat.sub(new_block, fm_block)
    else:
        new_fm = fm_block.rstrip() + "\n" + new_block + "\n"
    return "---" + new_fm + rest


def _extract_summary_section(note_text: str, max_lines: int = 12) -> str:
    """노트 본문에서 ## 요약 섹션 발췌 (다음 ## 헤더 또는 max_lines 까지)."""
    idx = note_text.find("## 요약")
    if idx < 0:
        return ""
    after = note_text[idx:].split("\n", 1)
    if len(after) < 2:
        return ""
    body = after[1]
    out_lines: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            break
        out_lines.append(line)
        if len(out_lines) >= max_lines:
            break
    return "\n".join(out_lines).strip()


def build_companion_note_llm(
    msg: dict,
    thread_id: str,
    msg_date: dt.date,
    attachments_relpaths: list[str],
    body_text: str,
) -> tuple[str | None, str]:
    """Generate frontmatter + ## 요약 via Claude haiku. ## 원본 메일 섹션은
    호출자가 raw body 로 직접 붙임 (LLM 변형 방지).
    Returns (note_text_without_raw_section, error_or_empty)."""
    headers = _headers_to_dict(msg.get("payload", {}).get("headers", []))
    frm = headers.get("from", "")
    to = headers.get("to", "")
    subject = headers.get("subject", "(제목 없음)")
    raw_date = headers.get("date", "")

    fm_guide = _vault_frontmatter_guide() or (
        "frontmatter 필수 필드: title, source, date, tags, sources(원본 있을 때), "
        "gmail_threadIds (배열)."
    )
    para_tree = _vault_para_tree() or "(vault tree 추출 실패 — sources/00_inbox/ 권장)"

    body_for_llm = body_text[:8000]  # cap LLM input

    sources_line = ""
    if attachments_relpaths:
        sources_line = "sources:\n" + "\n".join(f"  - {p}" for p in attachments_relpaths) + "\n"

    prompt = (
        "다음 Gmail 메일의 vault 동반 노트를 생성하라. **frontmatter + ## 요약 섹션만** 출력. "
        "## 원본 메일 섹션은 호출자가 추가하므로 절대 생성하지 마라.\n\n"
        f"vault frontmatter 표준 (gmail-capture.md §4 — 권위 정의):\n\n{fm_guide}\n\n"
        f"vault PARA 디렉토리 구조 (knowledge/ 하위, proposed_para_path 추론용):\n\n{para_tree}\n\n"
        "출력 사양:\n"
        "1. YAML frontmatter (--- 로 감쌈) — 다음 형식 그대로:\n"
        "---\n"
        "title: <메일 핵심을 25자 내외로 요약한 한글 제목>\n"
        "source: gmail\n"
        f"date: {msg_date.isoformat()}\n"
        "tags: [<3-5개 한글/영문 태그>]\n"
        f"{sources_line}"
        "gmail_threadIds:\n"
        f"  - {thread_id}\n"
        "proposed_para_path: <PARA 좌표만 (예: 02_areas/finance/). knowledge/, sources/ 같은 "
        "prefix 없이 좌표만. 확정 시 노트는 knowledge/<좌표>/, 첨부 있으면 sources/<좌표>/ 로 자동 분기. "
        "확신 없으면 빈 문자열 — staging(sources/00_inbox/) 유지>\n"
        "proposed_links:\n"
        "  - \"[[<vault에 있을 법한 관련 노트의 wikilink>]]\"\n"
        "  - \"[[<없으면 빈 리스트로>]]\"\n"
        "---\n\n"
        "2. 빈 줄 1개 후 `## 요약` 헤더 + audit trail 관점에서 중요한 5-15줄 핵심 정리. "
        "금액·일자·식별자·외부 링크·후속 액션 명시. 원본 본문 인용은 최소화 (호출자가 별도 첨부).\n\n"
        "응답은 위 두 부분만. 마크다운 코드블록(```)으로 감싸지 말고 본문 그대로. "
        "인사·해설·꼬리말 금지.\n\n"
        f"[메일 메타]\nfrom: {frm}\nto: {to}\ndate: {raw_date}\nsubject: {subject}\n"
        f"threadId: {thread_id}\n"
        f"attachments: {attachments_relpaths or '(없음)'}\n\n"
        f"[메일 본문 plain text]\n{body_for_llm}\n"
    )

    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-opus-4-7",
        "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return (None, "LLM 노트 생성 timeout")
    if r.returncode != 0:
        return (None, f"LLM exit={r.returncode}: {(r.stderr or '')[:200]}")

    out = (r.stdout or "").strip()
    # 코드블록 제거: 어떤 언어 hint 든 (```yaml, ```markdown, 등) 모두 처리.
    if out.startswith("```"):
        # drop first fence line entirely (everything up to first newline)
        nl = out.find("\n")
        out = out[nl + 1:] if nl >= 0 else ""
        # drop trailing fence
        if out.rstrip().endswith("```"):
            out = out.rstrip()[:-3]
        out = out.strip()
    if not out.startswith("---"):
        return (None, f"LLM 응답에 frontmatter 누락: {out[:150]}")
    if "## 요약" not in out:
        return (None, "LLM 응답에 ## 요약 섹션 누락")
    return (out, "")


def _companion_note_path(item: dict, msg_date: dt.date) -> pathlib.Path:
    """Deterministic note path: {date}_{from-slug}_{subject-slug}.md.
    Idempotent — same item always lands on same path so retries overwrite."""
    frm = item.get("from", "")
    # extract local-part of email if "Name <addr>" form
    m = re.search(r"<([^>]+)>", frm)
    addr = (m.group(1) if m else frm).split("@", 1)[0]
    from_slug = _slugify(addr, max_len=24)
    subj_slug = _slugify(item.get("subject", ""), max_len=40)
    fname = f"{msg_date.isoformat()}_{from_slug}_{subj_slug}.md"
    return VAULT_INBOX / fname


def _attachment_dir(thread_id: str) -> pathlib.Path:
    return VAULT_ATTACH_ROOT / thread_id


_LABEL_ID_CACHE: dict[str, "str | None"] = {}


def _resolve_label_id(name: str) -> "str | None":
    """사용자 라벨 *이름* → Gmail 라벨 *ID* 해석. 프로세스 캐시.
    2026-05-17 진단: gog 의 per-message `labelIds` 는 사용자 라벨을
    `Label_xxxx` **ID** 로 반환(검색·thread-level 만 이름). 따라서 멀티-
    메시지 대상 선택을 이름으로 매칭하면 항상 실패 → ID 해석 필요.
    시스템 라벨(SENT/INBOX 등)은 labels list 에 id==name 으로 있거나,
    없어도 호출부가 이름 매칭 병행하므로 무해. 실패 시 None."""
    if name in _LABEL_ID_CACHE:
        return _LABEL_ID_CACHE[name]
    lid: "str | None" = None
    out = gog_json("gmail", "labels", "list")
    if isinstance(out, dict):
        out = out.get("labels", out.get("items", []))
    for L in out or []:
        if isinstance(L, dict) and L.get("name") == name:
            lid = L.get("id")
            break
    _LABEL_ID_CACHE[name] = lid
    return lid


def _pick_target_in_thread(thread_payload: dict, label: str) -> dict | None:
    """라벨-인지 대상 메시지 선택 (2026-05-16 wrong-message 버그 수정;
    2026-05-17 ID-인지 매칭 보강).
    gog 검색은 thread 단위(메시지 id 없음) → 스레드 안에서 트리거 라벨이
    실제 붙은 메시지를 골라야 함. `_pick_target_message` 의 silent msgs[0]
    fallback 이 '엉뚱한(첫) 메시지로 노트 생성' 의 근원이었음.
    - 단일 메시지 스레드: 모호성 없음 → 그 메시지 (라벨 표현 무관·안전).
    - 멀티: labelIds 에 label 든 메시지(복수면 최신 internalDate).
      label='SENT' 는 시스템 라벨(신뢰) = 최신 보낸 메시지(=내 회신).
    - 못 찾으면 **None** (호출자가 skip+report — silent 오귀속 금지).
    **ID-인지 (2026-05-17)**: gog 가 per-message labelIds 를 사용자 라벨
    *ID* 로 반환하므로 이름만 매칭하면 멀티-메시지 항상 None(1·2·6·8 공통
    구조 결함이었음). `_resolve_label_id` 로 이름→ID 해석 후 *이름 OR ID*
    매칭. 선택 전략(라벨 붙은 그 메시지)은 불변 — 실측상 Dr. Ben 은 최신이
    아닌 특정 메시지(원 요청)에 라벨하므로 latest 폴백은 오귀속이라 채택 안 함."""
    thread = thread_payload.get("thread") or {}
    msgs = thread.get("messages") or []
    if not msgs:
        return None
    if len(msgs) == 1:
        return msgs[0]
    lid = _resolve_label_id(label)        # 사용자 라벨은 gog 가 ID 로 줌
    cands = [
        m for m in msgs
        if label in (m.get("labelIds") or [])
        or (lid is not None and lid in (m.get("labelIds") or []))
    ]
    if not cands:
        return None
    cands.sort(key=lambda m: int(m.get("internalDate", "0") or "0"))
    return cands[-1]


def propose_proceed(item: dict, *,
                    target_label: str | None = None) -> tuple[bool, str]:
    """Brainify Phase 1 — 본문 fetch + 동반 노트 작성 + atomic write. **라벨/archive 안 함.**
    item을 in-place로 채움: note_path / proposed_para_path / proposed_links / body_summary /
    attachments / confirm_status='pending_review'.
    target_label: 주어지면 스레드에서 그 라벨(또는 'SENT') 붙은 메시지를
      라벨-인지 선택 (multi-msg 스레드 정확). None 이면 legacy 동작 유지.
    Returns (ok, error_or_empty)."""
    thread_id = item.get("msg_id")
    if not thread_id:
        return (False, "thread_id 없음")

    attach_dir = _attachment_dir(thread_id)
    payload = fetch_thread_full(thread_id, out_dir=attach_dir)
    if payload is None:
        return (False, "thread fetch 실패")

    if target_label is not None:
        msg = _pick_target_in_thread(payload, target_label)
        if msg is None:
            return (False,
                    f"스레드에서 '{target_label}' 대상 메시지 식별 실패 — skip")
    else:
        msg = _pick_target_message(payload, thread_id)
    if not msg:
        return (False, "thread 에 메시지 없음")

    body_text = _extract_plain_text(msg.get("payload") or {})
    if not body_text:
        body_text = msg.get("snippet", "")

    attach_paths = _extract_attachment_paths(payload, attach_dir)
    if not attach_paths:
        try:
            attach_dir.rmdir()
        except OSError:
            pass
    rel_attach = [
        str(p.relative_to(VAULT_ROOT)) if p.is_absolute() and VAULT_ROOT in p.parents else str(p)
        for p in attach_paths
    ]

    msg_date = _parse_internal_date(msg.get("internalDate")) or now_kst().date()

    note_text, err = build_companion_note_llm(
        msg=msg,
        thread_id=thread_id,
        msg_date=msg_date,
        attachments_relpaths=rel_attach,
        body_text=body_text,
    )
    if note_text is None:
        return (False, f"노트 생성 실패: {err}")

    note_full = (
        note_text.rstrip()
        + "\n\n## 원본 메일\n\n```\n"
        + body_text.replace("```", "`​``")
        + "\n```\n"
    )

    note_path = _companion_note_path(item, msg_date)
    try:
        VAULT_INBOX.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(VAULT_INBOX))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(note_full)
        os.replace(tmp, note_path)
    except OSError as e:
        return (False, f"노트 파일 쓰기 실패: {e}")

    fm = _parse_frontmatter(note_full)
    proposed_links_raw = fm.get("proposed_links") or []
    proposed_links = [
        s.strip().strip('"').strip("'") for s in proposed_links_raw
        if isinstance(s, str) and s.strip() and "<" not in s  # drop unfilled placeholders
    ]
    item["note_path"] = str(note_path.relative_to(VAULT_ROOT))
    item["proposed_para_path"] = (fm.get("proposed_para_path") or "").strip().strip('"').strip("'")
    item["proposed_links"] = proposed_links
    item["body_summary"] = _extract_summary_section(note_text)
    item["attachments"] = rel_attach
    item["confirm_status"] = "pending_review"
    return (True, "")


def _normalize_para_coord(raw: str) -> str:
    """LLM 또는 사용자가 'knowledge/02_areas/finance/' / '02_areas/finance/' /
    'sources/02_areas/finance/' 등 다양한 형태로 줄 수 있어 정규화.
    PARA 좌표(prefix 없이 '02_areas/finance' 형태)를 반환.
    빈/staging 경로면 빈 문자열 → staging 유지 신호."""
    if not raw:
        return ""
    s = str(raw).strip().strip("/")
    for prefix in ("knowledge/", "sources/", "vault/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if not s or s.startswith("00_inbox"):
        return ""
    return s


_GOG_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{8,32}_[A-Za-z0-9_-]+_(.+)$")


def _strip_gog_prefix(name: str) -> str:
    """gog 가 첨부에 붙이는 '<threadId>_<shortAttachId>_' prefix 를 제거해 원본 파일명을 얻는다.
    패턴 안 맞으면 원본 그대로."""
    m = _GOG_PREFIX_RE.match(name)
    return m.group(1) if m else name


def _dedupe_attachments(attachments: list[str]) -> tuple[list[str], list[str]]:
    """첨부 list 에서 (original_filename, size) 일치하면 첫 1개만 keep, 나머지는 duped 로 분리.
    gog prefix 차이로 표층 이름이 달라도 동일 첨부면 dedupe.
    Returns (kept_rel, duped_rel) — 모두 vault root 기준 상대 경로."""
    seen: dict[tuple[str, int], str] = {}
    kept: list[str] = []
    duped: list[str] = []
    for rel in attachments:
        p = VAULT_ROOT / rel
        if not p.exists():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        key = (_strip_gog_prefix(p.name), size)
        if key in seen:
            duped.append(rel)
        else:
            seen[key] = rel
            kept.append(rel)
    return (kept, duped)


def _unique_dest(dst: pathlib.Path, src: pathlib.Path | None = None) -> pathlib.Path:
    """파일명 충돌 회피: 같은 이름 존재 시 stem.1.ext, stem.2.ext 순으로 시도.
    src 가 같은 파일을 가리키면(e.g. 멱등 재실행) 그대로 반환."""
    if not dst.exists():
        return dst
    if src is not None and src.exists() and dst.resolve() == src.resolve():
        return dst
    i = 1
    while True:
        cand = dst.with_name(f"{dst.stem}.{i}{dst.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _relocate_to_para(item: dict, para: str) -> tuple[bool, str]:
    """노트와 첨부를 PARA 폴더로 이동.
    - 노트   → knowledge/<para>/<note>.md
    - 첨부   → sources/<para>/<filename>  (충돌 시 .1, .2 회피)
    - frontmatter sources 필드를 새 경로 list 로 atomic 갱신
    - _attachments/<thread_id>/ 가 비면 정리
    item 의 note_path / attachments 를 in-place 갱신.
    para 가 빈 문자열이면 staging 유지 (no-op success)."""
    if not para:
        return (True, "")
    note_rel = item.get("note_path")
    if not note_rel:
        return (False, "note_path 없음")
    src_note = VAULT_ROOT / note_rel
    if not src_note.exists():
        return (False, f"노트 파일 없음: {note_rel}")

    # 1. 첨부 dedupe — 동일 (filename, size) 는 1개만 PARA 로, 나머지는 _attachments/_dup/ 로 격리
    raw_attachments = item.get("attachments") or []
    kept_attachments, duped_attachments = _dedupe_attachments(raw_attachments)

    if duped_attachments:
        dup_dir = VAULT_ATTACH_ROOT / "_dup"
        dup_dir.mkdir(parents=True, exist_ok=True)
        for rel in duped_attachments:
            src = VAULT_ROOT / rel
            if not src.exists():
                continue
            dst = _unique_dest(dup_dir / src.name, src)
            try:
                os.replace(src, dst)
            except OSError:
                pass  # best-effort, 회복 가능

    # 2. kept 첨부들을 PARA 폴더로 이동 — gog prefix 제거된 깨끗한 파일명으로
    new_attach_rel: list[str] = []
    for attach_rel in kept_attachments:
        src_attach = VAULT_ROOT / attach_rel
        if not src_attach.exists():
            continue
        dst_dir = VAULT_ROOT / "sources" / para
        dst_dir.mkdir(parents=True, exist_ok=True)
        clean_name = _strip_gog_prefix(src_attach.name)
        dst_attach = _unique_dest(dst_dir / clean_name, src_attach)
        try:
            os.replace(src_attach, dst_attach)
        except OSError as e:
            return (False, f"첨부 이동 실패 ({src_attach.name}): {e}")
        new_attach_rel.append(str(dst_attach.relative_to(VAULT_ROOT)))

    # 2. 노트 frontmatter sources 필드 갱신 후 노트 이동 (atomic)
    try:
        text = src_note.read_text(encoding="utf-8")
    except OSError as e:
        return (False, f"노트 읽기 실패: {e}")
    if new_attach_rel:
        text = _replace_fm_field_list(text, "sources", new_attach_rel)

    dst_note_dir = VAULT_ROOT / "knowledge" / para
    dst_note_dir.mkdir(parents=True, exist_ok=True)
    dst_note = _unique_dest(dst_note_dir / src_note.name, src_note)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(dst_note_dir))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, dst_note)
        if dst_note != src_note:
            try:
                src_note.unlink()
            except OSError:
                pass
    except OSError as e:
        return (False, f"노트 이동 실패: {e}")

    # 3. _attachments/<thread_id>/ 빈 디렉토리 정리
    tid = item.get("msg_id") or ""
    if tid:
        attach_dir = VAULT_ATTACH_ROOT / tid
        if attach_dir.exists():
            try:
                attach_dir.rmdir()  # 비어있을 때만 성공
            except OSError:
                pass

    item["note_path"] = str(dst_note.relative_to(VAULT_ROOT))
    item["attachments"] = new_attach_rel
    return (True, "")


# ============================================================================
# Approval execution
# ============================================================================


# ============================================================================
# Queue helpers — proceed 항목별 1:1 confirm 흐름
# ============================================================================

def _proceed_items(plan: dict) -> list[dict]:
    return [it for it in plan.get("items", []) if it.get("category") == "proceed"]


def _find_item_by_thread(plan: dict, thread_id: str) -> dict | None:
    for it in plan.get("items", []):
        if it.get("msg_id") == thread_id:
            return it
    return None


# ============================================================================
# Formatting — proposal / label-only / completion messages
# ============================================================================

def _short_subject(s: str, n: int = 50) -> str:
    s = s or "(제목 없음)"
    return s if len(s) <= n else s[: n - 1] + "…"


# ============================================================================
# Snooze
# ============================================================================


# ============================================================================
# Subcommand handlers
# ============================================================================


def _search_vault_context(item: dict, max_files: int = 3) -> str:
    """vault knowledge/ 에서 메일 제목 키워드 + 발신자 매칭 노트 상위 max_files 의 ## 요약 발췌."""
    subject = item.get("subject", "") or ""
    frm = item.get("from", "") or ""
    addr_m = re.search(r"<([^>]+)>", frm)
    addr_local = (addr_m.group(1) if addr_m else frm).split("@", 1)[0]
    tokens = [t for t in re.split(r"[\s\[\]()/,:;!?\"'\-—–.]+", subject) if len(t) >= 2]
    knowledge_dir = VAULT_ROOT / "knowledge"
    if not knowledge_dir.exists():
        return ""
    pat_parts = [re.escape(t) for t in tokens[:5]]
    if addr_local:
        pat_parts.append(re.escape(addr_local))
    if not pat_parts:
        return ""
    pattern = "|".join(pat_parts)
    try:
        r = subprocess.run(
            ["rg", "-l", "--max-count", "1", "-i", pattern, str(knowledge_dir)],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    if r.returncode not in (0, 1):
        return ""
    files = (r.stdout or "").strip().splitlines()
    if not files:
        return ""
    snippets: list[str] = []
    for fp in files[:max_files]:
        try:
            text = pathlib.Path(fp).read_text(encoding="utf-8")
        except OSError:
            continue
        snip = _extract_summary_section(text, max_lines=8)
        if not snip:
            continue
        try:
            rel = pathlib.Path(fp).relative_to(VAULT_ROOT)
        except ValueError:
            rel = pathlib.Path(fp)
        snippets.append(f"[{rel}]\n{snip}")
    return "\n\n".join(snippets)


def _search_prior_threads(item: dict, max_threads: int = 3) -> str:
    """같은 발신자 직전 thread max_threads 개의 subject + snippet 요약."""
    frm = item.get("from", "") or ""
    addr_m = re.search(r"<([^>]+)>", frm)
    addr = (addr_m.group(1) if addr_m else frm).strip()
    if not addr:
        return ""
    items = gog_json("gmail", "search", f"from:{addr}", "--max", str(max_threads + 1))
    if not items:
        return ""
    if isinstance(items, dict):
        items = items.get("messages", items.get("items", []))
    out: list[str] = []
    cur_id = item.get("msg_id")
    for m in items or []:
        if m.get("id") == cur_id or m.get("threadId") == cur_id:
            continue
        subj = m.get("subject", "")
        snip = (m.get("snippet", "") or "")[:200]
        date = m.get("date", "")
        out.append(f"- [{date}] {subj}\n  {snip}")
        if len(out) >= max_threads:
            break
    return "\n".join(out)


def _attach_draft_to_note(item: dict, draft_id: str) -> None:
    """노트 frontmatter 에 gmail_draft_id 기록 (best-effort)."""
    note_rel = item.get("note_path")
    if not note_rel or not draft_id:
        return
    note_path = VAULT_ROOT / note_rel
    if not note_path.exists():
        return
    try:
        text = note_path.read_text(encoding="utf-8")
        text = _replace_fm_field(text, "gmail_draft_id", draft_id)
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(note_path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, note_path)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _extract_schedule_from_email(item: dict, now: dt.datetime) -> dict | None:
    """노트의 ## 요약 + 원본 메일에서 캘린더 이벤트 데이터 추출 (Opus 4.7).
    Returns dict {summary, start, end, location, all_day} or None on failure.
    start/end 는 RFC3339 (Asia/Seoul). all_day=True 면 start/end 는 'YYYY-MM-DD'."""
    note_rel = item.get("note_path")
    if not note_rel:
        return None
    note_path = VAULT_ROOT / note_rel
    if not note_path.exists():
        return None
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return None
    summary_section = _extract_summary_section(text, max_lines=30) or ""
    body_section = ""
    body_marker = text.find("## 원본 메일")
    if body_marker >= 0:
        body_section = text[body_marker: body_marker + 4000]

    today_iso = now.date().isoformat()
    prompt = (
        f"오늘 날짜: {today_iso} (KST, +09:00)\n\n"
        "다음 메일에서 캘린더 이벤트 1개를 추출하라. 명확한 시작 일시가 있어야 함.\n"
        "- 시작·종료가 모두 시간 포함이면 RFC3339 (예: '2026-05-15T14:00:00+09:00').\n"
        "- 시작은 있는데 종료 없음 → 종료는 시작 + 1시간.\n"
        "- 시간 정보 없이 날짜만 → all_day=true, start/end='YYYY-MM-DD' (end 는 start 동일).\n"
        "- 명확한 일시가 없거나 모호하면 null 반환.\n"
        "- summary 는 한국어 한 줄. location 은 본문에 명시된 경우만 (없으면 빈 문자열).\n"
        "응답은 JSON 객체 한 줄만. 다른 텍스트·코드블록·해설 금지.\n"
        '예: {"summary":"학회 이사회","start":"2026-05-15T14:00:00+09:00",'
        '"end":"2026-05-15T16:00:00+09:00","location":"서울대병원 의생명연구원","all_day":false}\n'
        '추출 불가: {"event": null}\n\n'
        f"[메일 제목] {item.get('subject', '')}\n"
        f"[발신] {item.get('from', '')}\n"
        f"[요약]\n{summary_section[:2500]}\n\n"
        f"[본문 발췌]\n{body_section[:2500]}\n"
    )
    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-opus-4-7",
        "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if out.startswith("```"):
        nl = out.find("\n")
        out = out[nl + 1:] if nl >= 0 else ""
        if out.rstrip().endswith("```"):
            out = out.rstrip()[:-3]
        out = out.strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        try:
            s = out.index("{")
            e = out.rindex("}") + 1
            parsed = json.loads(out[s:e])
        except (ValueError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("event") is None and "start" not in parsed:
        return None
    start = parsed.get("start")
    if not isinstance(start, str) or not start.strip():
        return None
    return {
        "summary": (parsed.get("summary") or item.get("subject") or "").strip() or "(제목 없음)",
        "start": start.strip(),
        "end": (parsed.get("end") or start).strip(),
        "location": (parsed.get("location") or "").strip(),
        "all_day": bool(parsed.get("all_day")),
    }


def _attach_schedule_to_note(item: dict, event_id: str, ev: dict) -> None:
    """노트 frontmatter 에 calendar_event_id / calendar_start 기록 (best-effort)."""
    note_rel = item.get("note_path")
    if not note_rel:
        return
    note_path = VAULT_ROOT / note_rel
    if not note_path.exists():
        return
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return
    text = _replace_fm_field(text, "calendar_event_id", event_id)
    text = _replace_fm_field(text, "calendar_start", ev.get("start", ""))
    # 사람 기준 포함 종료일 (Google 배타적 end 가 아님 — audit 추적용)
    text = _replace_fm_field(text, "calendar_end",
                             ev.get("end", ev.get("start", "")))
    text = _replace_fm_field(text, "calendar_all_day",
                             "true" if ev.get("all_day") else "false")
    try:
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(note_path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, note_path)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass


def _create_calendar_event(ev: dict, thread_id: str, note_rel: str) -> tuple[str | None, str]:
    """Google Calendar primary 에 이벤트 생성. Returns (event_id, error).
    주의: all-day 이벤트의 end.date 는 Google API 에서 **EXCLUSIVE(배타적)** —
    사람 기준 포함 종료일을 그대로 넘기면 하루 짧게 표시됨(예 9.8~9.12 →
    9.8~9.11). all_day 면 종료일 +1 (단일일 포함). 시간 있는 이벤트는 무관."""
    description = (
        f"Gmail thread: https://mail.google.com/mail/u/0/#all/{thread_id}\n"
        f"Vault note: {note_rel}"
    )
    start = ev["start"]
    end = ev.get("end") or start
    if ev.get("all_day"):
        try:
            end = (dt.date.fromisoformat(end[:10])
                   + dt.timedelta(days=1)).isoformat()
        except (ValueError, TypeError):
            pass  # 파싱 실패 → 원본 유지 (best-effort)
    args = [
        "calendar", "create", "primary",
        "--summary", ev.get("summary") or "(제목 없음)",
        "--from", start,
        "--to", end,
        "--description", description,
    ]
    if ev.get("location"):
        args.extend(["--location", ev["location"]])
    if ev.get("all_day"):
        args.append("--all-day")
    out = gog_json(*args)
    if isinstance(out, dict) and out.get("id"):
        return (out["id"], "")
    return (None, f"calendar create 응답 비정상: {out}")


def _extract_task_from_email(item: dict, now: dt.datetime) -> dict:
    """노트 ## 요약 + 원본 메일에서 할 일(action) 1개 추출 (Opus 4.7).
    `6 할일` 은 Dr. Ben 이 이미 '할 일' 로 분류한 메일 — 추출 실패해도
    제목을 fallback 으로 항상 task 1개를 만든다 (일정과 달리 None 없음).
    Returns {title, due, detail}. due 는 '' 또는 'YYYY-MM-DD'."""
    subj = (item.get("subject") or "").strip()
    fallback = {"title": subj or "(제목 없음)", "due": "", "detail": ""}
    note_rel = item.get("note_path")
    if not note_rel:
        return fallback
    note_path = VAULT_ROOT / note_rel
    if not note_path.exists():
        return fallback
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    summary_section = _extract_summary_section(text, max_lines=30) or ""
    body_section = ""
    body_marker = text.find("## 원본 메일")
    if body_marker >= 0:
        body_section = text[body_marker: body_marker + 4000]
    today_iso = now.date().isoformat()
    prompt = (
        f"오늘 날짜: {today_iso} (KST, +09:00)\n\n"
        "다음 메일이 Dr. Ben 에게 요구하는 '할 일'(행동) 1개를 추출하라.\n"
        "- title: 동사로 시작하는 한국어 한 줄 행동 "
        "(예 '학회 초록 제출', '계약서 검토 후 회신').\n"
        "- due: 마감일이 본문에 명시되면 'YYYY-MM-DD', 없으면 빈 문자열.\n"
        "- detail: 보조 맥락 한두 줄 (없으면 빈 문자열).\n"
        "응답은 JSON 객체 한 줄만. 다른 텍스트·코드블록·해설 금지.\n"
        '예: {"title":"학회 초록 제출","due":"2026-05-31",'
        '"detail":"KSNM 춘계 온라인 제출 시스템"}\n\n'
        f"[메일 제목] {subj}\n"
        f"[발신] {item.get('from', '')}\n"
        f"[요약]\n{summary_section[:2500]}\n\n"
        f"[본문 발췌]\n{body_section[:2500]}\n"
    )
    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-opus-4-7",
        "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True,
                           text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return fallback
    if r.returncode != 0:
        return fallback
    out = (r.stdout or "").strip()
    if out.startswith("```"):
        nl = out.find("\n")
        out = out[nl + 1:] if nl >= 0 else ""
        if out.rstrip().endswith("```"):
            out = out.rstrip()[:-3]
        out = out.strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        try:
            s = out.index("{")
            e = out.rindex("}") + 1
            parsed = json.loads(out[s:e])
        except (ValueError, json.JSONDecodeError):
            return fallback
    if not isinstance(parsed, dict):
        return fallback
    title = (parsed.get("title") or subj or "").strip() or "(제목 없음)"
    due = (parsed.get("due") or "").strip()
    if due and not _DATE_RE.match(due):
        due = ""          # LLM 형식 일탈 → 마감 없음 취급 (할 일은 마감 선택)
    return {"title": title, "due": due,
            "detail": (parsed.get("detail") or "").strip()}


def _attach_task_to_note(item: dict, task_id: str, te: dict) -> None:
    """노트 frontmatter 에 google_task_id / google_task_due 기록 (best-effort).
    schedule 핸들러의 calendar_event_id 가드와 평행 — §11.5 cron 핸들러 내부
    일관성 유지 (§10 인터랙티브 경로의 gtask_id·복수형 google_task_ids 와는
    별개의 무인 cron 단발 필드)."""
    note_rel = item.get("note_path")
    if not note_rel:
        return
    note_path = VAULT_ROOT / note_rel
    if not note_path.exists():
        return
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return
    text = _replace_fm_field(text, "google_task_id", task_id)
    text = _replace_fm_field(text, "google_task_due", te.get("due", ""))
    try:
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(note_path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, note_path)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass


def cmd_status(state: dict, now: dt.datetime) -> int:
    plan = state.get("pending_plan")
    su = state.get("snooze_until")
    awaiting = state.get("awaiting_reply") or []
    gtasks_list_id = state.get("gtasks_list_id") or "(미설정)"
    print(f"[gws-assistant status] now={now.isoformat()}", file=sys.stderr)
    print(f"  last_checked: {state.get('last_checked', '')}", file=sys.stderr)
    print(f"  snooze_until: {su or '(없음)'}", file=sys.stderr)
    print(f"  gtasks_list_id: {gtasks_list_id}", file=sys.stderr)
    print(f"  awaiting_reply: {len(awaiting)}건", file=sys.stderr)
    for a in awaiting[:5]:
        subj = (a.get("subject") or "")[:40]
        print(f"    · {a.get('msg_id', '')[:12]}… '{subj}' drafted={a.get('drafted_at', '')[:16]}",
              file=sys.stderr)
    if plan:
        items = plan.get("items", [])
        cats = {}
        for it in items:
            cats[it["category"]] = cats.get(it["category"], 0) + 1
        print(f"  pending_plan: {len(items)}건 — {cats}", file=sys.stderr)
        print(f"    plan_id: {plan.get('plan_id', '')}", file=sys.stderr)
        print(f"    updated_at: {plan.get('updated_at', '')}", file=sys.stderr)
        print(f"    last_announced_msg_ids: {len(plan.get('last_announced_msg_ids', []))}건", file=sys.stderr)
    else:
        print(f"  pending_plan: (없음)", file=sys.stderr)
    return 0


# ============================================================================
# Poll (default mode)
# ============================================================================

def _poll_awaiting_replies(state: dict, now: dt.datetime) -> str:
    """awaiting_reply 큐의 각 항목에 대해 발송 감지 (drafted_at 이후 SENT 라벨 메시지).
    감지 시: 트리거 라벨(8/4/5/7) 제거 + `9 완료` 부착 promote + 노트 frontmatter 에 replied_at 기록 + 큐에서 제거.
    Returns 사용자에게 보고할 한 줄 요약 메시지 (또는 발송 미감지 시 빈 문자열)."""
    queue = state.get("awaiting_reply") or []
    if not queue:
        return ""
    promoted: list[dict] = []
    still_pending: list[dict] = []
    for entry in queue:
        thread_id = entry.get("msg_id")
        if not thread_id:
            continue
        try:
            drafted_dt = dt.datetime.fromisoformat(entry.get("drafted_at", ""))
            drafted_ms = int(drafted_dt.timestamp() * 1000)
        except (ValueError, TypeError):
            drafted_ms = 0
        payload = gog_json("gmail", "thread", "get", thread_id)
        if not isinstance(payload, dict):
            still_pending.append(entry)
            continue
        thread = payload.get("thread") or {}
        msgs = thread.get("messages", []) if isinstance(thread, dict) else []
        sent_after_draft = False
        for m in msgs:
            if "SENT" not in (m.get("labelIds") or []):
                continue
            try:
                ts = int(m.get("internalDate", "0") or "0")
            except (ValueError, TypeError):
                ts = 0
            if ts >= drafted_ms - 1000:  # 1초 토너런스 — drafts create 시점과 sent 시점 동일 millisecond
                sent_after_draft = True
                break
        if not sent_after_draft:
            still_pending.append(entry)
            continue
        # phase-2 promote (new-only, 2026-05-17): src_label = 트리거 라벨
        # (8/4/5/7), terminal = `9 완료`. 레거시 grandfather 경로 삭제
        # (레거시 reply 생성기 제거 → src_label 없는 엔트리 영구 0).
        _rm = entry.get("src_label")
        _add = entry.get("terminal") or LABEL_DONE_9
        if not _rm:
            # 구 포맷/비정상 엔트리 — 엉뚱한 라벨 제거 방지: skip
            still_pending.append(entry)
            continue
        ok, _err = gog_call(
            "gmail", "labels", "modify", thread_id,
            "--add", _add, "--remove", f"{_rm},INBOX",
        )
        if not ok:
            still_pending.append(entry)
            continue
        # 노트 frontmatter 에 replied_at 기록 (best-effort)
        note_rel = entry.get("vault_note_path")
        if note_rel:
            note_path = VAULT_ROOT / note_rel
            if note_path.exists():
                try:
                    text = note_path.read_text(encoding="utf-8")
                    text = _replace_fm_field(text, "replied_at",
                                             now.isoformat(timespec="seconds"))
                    fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(note_path.parent))
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(text)
                    os.replace(tmp, note_path)
                except OSError:
                    try:
                        os.unlink(tmp)
                    except (OSError, NameError):
                        pass
        promoted.append(entry)
    state["awaiting_reply"] = still_pending
    if not promoted:
        return ""
    lines = [f"[발송 감지 → `{LABEL_DONE_9}`] 자동 종결 {len(promoted)}건"]
    for e in promoted:
        subj = (e.get("subject") or "").strip() or "(제목 없음)"
        lines.append(f"  · {_short_subject(subj, 50)}")
    lines.append("")
    return "\n".join(lines)


# ============================================================================
# §11.3 `1 저장` 완전무인 파이프라인 (gmail-capture.md §11 권위)
# ============================================================================
#
# 크래시-안전 순서: 노트 staging write → (첨부+노트) PARA 이동 → 라벨 변경(commit
# point, strictly 마지막). 라벨 변경 전 어디서 죽어도 메일이 `1 저장` 잔류 →
# 다음 사이클이 threadId 가드로 재진입해 멱등 복구.

# ── 파서 추상화 (§11.3 호출부 범용화) ──
# 파이프라인은 구체 파서가 아니라 parse_attachment() 단일 인터페이스만 호출.
# 타입별 dispatch 레지스트리 — 현재 internal 만. 추후 Docling/MinerU 를
# _register_parser("pdf", fn) 로 등록하면 파이프라인 코드 불변 (§11.6).
PARSER_REGISTRY: dict[str, "callable"] = {}


def _register_parser(ext: str, fn) -> None:
    PARSER_REGISTRY[ext.lower().lstrip(".")] = fn


def _parser_internal(path: pathlib.Path) -> dict:
    """internal provider = Claude 내장 read (§11.3). 단일 파일을 마크다운으로 추출.
    실패는 hard-fail 없이 warnings 로 degrade. 반환 계약은 parse_attachment 와 동일."""
    base = {"text_markdown": "", "parser_id": "internal",
            "parser_version": PARSER_VERSION, "warnings": []}
    if not path.exists():
        base["warnings"] = [f"파일 없음: {path.name}"]
        return base
    prompt = (
        f"파일 경로: {path}\n"
        "이 파일의 텍스트 내용을 깨끗한 마크다운으로 그대로 추출해 출력하라. "
        "표는 마크다운 표로. 해설·요약·인사·꼬리말 절대 금지 — 추출 결과만."
    )
    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-haiku-4-5-20251001", "--allowedTools", "Read",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True,
                            text=True, timeout=180)
    except subprocess.TimeoutExpired:
        base["warnings"] = ["internal parser timeout"]
        return base
    if r.returncode != 0:
        base["warnings"] = [f"internal parser exit={r.returncode}"]
        return base
    txt = (r.stdout or "").strip()
    base["text_markdown"] = txt[:12000]
    if not txt:
        base["warnings"] = ["빈 추출"]
    return base


def parse_attachment(path: pathlib.Path) -> dict:
    """§11.3 단일 인터페이스. 타입별 dispatch → 등록 파서, 미등록 → internal.
    반환: {text_markdown, parser_id, parser_version, warnings[]}."""
    ext = path.suffix.lower().lstrip(".")
    fn = PARSER_REGISTRY.get(ext, _parser_internal)
    try:
        out = fn(path)
        # 계약 보장 — provider 가 키를 빠뜨려도 안전
        out.setdefault("text_markdown", "")
        out.setdefault("parser_id", "internal")
        out.setdefault("parser_version", PARSER_VERSION)
        out.setdefault("warnings", [])
        return out
    except Exception as e:  # noqa: BLE001 — 어떤 파서 예외도 파이프라인 죽이지 않음
        return {"text_markdown": "", "parser_id": "internal",
                "parser_version": PARSER_VERSION,
                "warnings": [f"parse 예외 fallback: {type(e).__name__}: {e}"]}


# ── threadId 멱등 가드 (§11.3 step 2) ──

def _existing_note_for_thread(thread_id: str) -> tuple[pathlib.Path | None, bool]:
    """knowledge/ + sources/00_inbox/ 에서 frontmatter gmail_threadIds 에
    thread_id 가 든 노트 탐색. 반환 (note_path|None, relocated).
    relocated=True 면 staging(00_inbox) 밖 = PARA 배치까지 끝난 상태."""
    roots = [str(VAULT_ROOT / "knowledge"), str(VAULT_INBOX)]
    try:
        r = subprocess.run(
            ["grep", "-rlF", "--include=*.md", thread_id, *roots],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return (None, False)
    for line in (r.stdout or "").splitlines():
        p = pathlib.Path(line.strip())
        if not p.is_file():
            continue
        try:
            fm = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        tids = fm.get("gmail_threadIds") or []
        if isinstance(tids, list) and thread_id in tids:
            relocated = (VAULT_INBOX not in p.parents) and (p.parent != VAULT_INBOX)
            return (p, relocated)
    return (None, False)


# PHI 점검 없음 (2026-05-16 Dr. Ben): 이 Gmail 계정엔 환자정보 송수신
# 자체가 없어 backstop 무가치(오탐 리스크만). CLAUDE.md 2026-04-24
# "PHI 자동 가드 삭제(Ben 이 법 숙지)" 결정과 일관 — 재도입 금지.


def _ensure_label_9() -> None:
    """`9 완료` 라벨 best-effort 생성 (이미 있으면 gog 가 에러 — 무시).
    실제 부착 실패는 commit 단계에서 per-item 으로 surface 된다."""
    gog_call("gmail", "labels", "create", LABEL_DONE_9)


def _commit_action_label(thread_id: str, src_label: str) -> tuple[bool, str]:
    """§11 commit point — strictly 마지막. src_label 제거 + `9 완료` 부착
    + inbox 제거 (이미 archived 면 무해). 1~8 공용 (src_label 만 다름).
    주의: gog `--remove` 는 콤마구분 단일 플래그 — 반복 금지 (반복 시 1개만 적용)."""
    return gog_call(
        "gmail", "labels", "modify", thread_id,
        "--add", LABEL_DONE_9, "--remove", f"{src_label},INBOX",
    )


def _save_inject_provenance(note_rel: str, parser_id: str,
                            parser_version: str, warnings: list[str]) -> None:
    """staging 노트 frontmatter 에 para_review/parser provenance 주입 (atomic)."""
    p = VAULT_ROOT / note_rel
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    text = _replace_fm_field(text, "para_review", "pending")
    text = _replace_fm_field(text, "parser_id", parser_id)
    text = _replace_fm_field(text, "parser_version", parser_version)
    if warnings:
        text = _replace_fm_field(
            text, "parse_warnings",
            "[" + ", ".join(w.replace(",", " ") for w in warnings) + "]")
    try:
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(p.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass


def _save_append_parsed(note_rel: str, parsed: list[tuple[str, dict]]) -> None:
    """추출된 첨부 마크다운을 staging 노트 본문에 `## 첨부 파싱` 섹션으로 append."""
    if not parsed:
        return
    p = VAULT_ROOT / note_rel
    if not p.exists():
        return
    blocks = ["\n\n## 첨부 파싱 (parser: internal v" + PARSER_VERSION + ")\n"]
    for name, out in parsed:
        md = (out.get("text_markdown") or "").strip()
        warns = out.get("warnings") or []
        blocks.append(f"\n### {name}\n")
        if md:
            blocks.append("\n" + md.replace("```", "`​``") + "\n")
        else:
            blocks.append(f"\n(추출 실패 — {', '.join(warns) or '내용 없음'})\n")
    try:
        text = p.read_text(encoding="utf-8") + "".join(blocks)
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(p.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass


def _label_query(label: str) -> str:
    """Gmail `q` 의 `label:` 연산자 형식. 2026-05-17 실측: `+` 포함 라벨은
    인용형 `label:"…"` 이 0건(`+` 가 q 파서서 깨짐), **공백→하이픈 unquoted**
    (`label:3-일정+할일`)이 적중. `+` 없는 라벨(1·2·6·8)은 인용형이 검증됨.
    검증된 형식만 사용해 회귀 방지 위해 분기 (4·5·7 도 `+` 라 선재 커버)."""
    if "+" in label:
        return "label:" + label.replace(" ", "-")
    return f'label:"{label}"'


def _run_label_drain(state: dict, now: dt.datetime, *,
                     label: str, tag: str,
                     extra_action=None,
                     commit_fn=None,
                     dry_run: bool = False,
                     limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """§11 액션-라벨 완전무인 드레인 코어 (1~8 공용).
    label   : 트리거·제거 라벨 (예 '1 저장', '2 일정')
    tag     : 보고 머리표
    extra_action: 신규·재개 경로에서 노트 staging 직후·PARA 이동 전에 실행할
                  콜백 (item, now)->(ok, err). None 이면 없음.
                  **idempotent 필수** — 크래시 후 재개 시 재호출되므로
                  (예: 이미 만든 캘린더 이벤트 재생성 금지).
    commit_fn: 최종 커밋 콜백 (thread_id, label)->(ok, err). None → terminal
               `_commit_action_label`(라벨 제거 + `9 완료`). `8 회신` 은
               2단계 비-terminal 이라 `_commit_reply_label`(archive 만, 라벨
               유지 → 발송 감지 후 `_poll_awaiting_replies` 가 `9 완료` promote).
    awaiting_reply 큐에 있는 tid 는 skip — 초안 발송 대기 중인 `8 회신` 항목이
    매 사이클 재처리·재공지되는 것 방지 (1/2/6 은 큐 비어 무영향).
    반환 (full, problem): full=전체요약(터미널), problem=오류만(cron→Telegram).
    `-label:"9 완료"` 미포함 — commit 부분실패 stuck 도 가드로 자가치유.
    Telegram 정책: 성공 침묵, 오류만 (`8 회신` 만 §11.5 명문화 예외 — 호출
    래퍼 `_run_reply_label_drain` 이 사이클당 1건 요약을 problem 에 합성)."""
    if commit_fn is None:
        commit_fn = _commit_action_label
    _awaiting_ids = {e.get("msg_id")
                     for e in (state.get("awaiting_reply") or [])
                     if e.get("msg_id")}
    query = _label_query(label)
    results = gog_json("gmail", "search", query, "--max", str(limit))
    if results is None:
        _m = f"[{tag} 드레인] gmail 검색 실패 (gog OAuth?)\n"
        return (_m, _m)
    if isinstance(results, dict):
        results = results.get("messages", results.get("items", []))
    if not results:
        # 수동 dry-run 은 0건도 명시 (cron 비-dry 는 침묵 유지)
        if dry_run:
            return (f"[{tag} 드레인 — dry-run] 처리 대상 없음 (검색 0건)\n", "")
        return ("", "")

    done: list[str] = []
    repaired: list[str] = []
    errors: list[str] = []
    planned: list[str] = []

    if not dry_run:
        _ensure_label_9()

    for m in results:
        tid = m.get("id")
        subj = (m.get("subject") or "").strip() or "(제목 없음)"
        short = _short_subject(subj, 50)
        if not tid:
            continue
        # 발송 대기 중(`8 회신` 초안 등록됨) → skip. 매 사이클 재처리·재공지
        # 방지. state 유실 시 큐 비어 → 가드 경로로 재진입해 멱등 복구(수렴).
        if tid in _awaiting_ids:
            continue

        # ── tick 예산 게이트 (FailoverError 방어) ──
        # 비-dry 실처리에서 예산 소진 시 이 메일·이하 전부 다음 tick 으로
        # 이월 (라벨/노트존재 가드가 멱등 보존). 소비는 아래 무거운
        # 브랜치(staging 재개·신규)에서만 — 값싼 라벨복구는 starve 안 함.
        if not dry_run and not _tick_budget_left():
            break

        # ── threadId 멱등 가드 ──
        existing, relocated = _existing_note_for_thread(tid)
        if existing is not None and relocated:
            # 노트+이동 끝, 라벨만 미완 → **액션-인지 복구** (2026-05-17 Dr. Ben).
            # 과거: relocated 노트 존재 = extra_action 도 끝났다고 가정하고
            # 라벨만 commit → 다른 핸들러/무액션 캡처가 만든 노트면 이 라벨의
            # 액션(Task/이벤트)이 조용히 누락되는 크로스-핸들러 사각지대.
            # 수정: extra_action 을 호출(이미 멱등 — frontmatter 의
            # google_task_id/calendar_event_id 마커 있으면 self-skip, 없으면
            # 보강). extra_action 자체가 곧 '액션-인지 술어'. 노트·첨부·PARA
            # 이동은 끝났으므로 그 단계만 skip (재생성/재배치 안 함).
            if dry_run:
                _aw = " (액션 보강 idempotent)" if extra_action else ""
                planned.append(f"· {short} → [복구]{_aw} + 라벨 commit")
                continue
            if extra_action is not None:
                ritem = {"msg_id": tid, "from": m.get("from", ""),
                         "subject": subj,
                         "note_path": str(existing.relative_to(VAULT_ROOT))}
                ok, err = extra_action(ritem, now)   # 멱등: 마커 있으면 skip
                if not ok:
                    errors.append(f"{short} (액션 보강 실패: {err})")
                    continue
            ok, err = commit_fn(tid, label)
            (repaired if ok else errors).append(
                short if ok else f"{short} (라벨 복구 실패: {err})")
            continue

        item: dict = {"msg_id": tid, "from": m.get("from", ""),
                      "subject": subj}

        if existing is not None and not relocated:
            # staging 노트 존재 (이동 전 크래시) → extra_action 보강 + 재배치 + commit
            if dry_run:
                planned.append(f"· {short} → [재개] staging 노트 재배치+commit")
                continue
            _spend_tick_budget()   # 무거운 재개 1건 소비
            try:
                fm = _parse_frontmatter(existing.read_text(encoding="utf-8"))
            except OSError:
                fm = {}
            item["note_path"] = str(existing.relative_to(VAULT_ROOT))
            item["attachments"] = fm.get("sources") or []
            if extra_action is not None:
                ok, err = extra_action(item, now)   # idempotent
                if not ok:
                    errors.append(f"{short} ({err})")
                    continue
            para = _normalize_para_coord(fm.get("proposed_para_path") or "")
            if para:
                ok, err = _relocate_to_para(item, para)
                if not ok:
                    errors.append(f"{short} (재배치 실패: {err})")
                    continue
            ok, err = commit_fn(tid, label)
            (done if ok else errors).append(
                short if ok else f"{short} (commit 실패: {err})")
            continue

        # ── 신규 (PHI 점검 없음) ──
        if dry_run:
            planned.append(f"· {short} → [신규] 노트 생성+배치+commit")
            continue
        _spend_tick_budget()   # 무거운 신규 1건 소비

        # ── 본문 fetch + 노트 생성 + staging write ──
        # target_label=label: gog 검색이 thread 단위라, 스레드 안에서 이
        # 라벨이 실제 붙은 메시지를 선택 (multi-msg wrong-message 버그 수정).
        ok, err = propose_proceed(item, target_label=label)
        if not ok:
            errors.append(f"{short} ({err})")
            continue
        note_rel = item.get("note_path")

        # 첨부 파서 dispatch → provenance + 본문 append (§11.3)
        parsed: list[tuple[str, dict]] = []
        all_warns: list[str] = []
        for arel in item.get("attachments") or []:
            ap = VAULT_ROOT / arel
            out = parse_attachment(ap)
            parsed.append((_strip_gog_prefix(ap.name), out))
            all_warns.extend(out.get("warnings") or [])
        if note_rel:
            _save_append_parsed(note_rel, parsed)
            _save_inject_provenance(note_rel, "internal",
                                    PARSER_VERSION, all_warns)

        # 라벨별 추가 액션 (예: 2 일정 = Calendar 이벤트). idempotent.
        if extra_action is not None:
            ok, err = extra_action(item, now)
            if not ok:
                errors.append(f"{short} ({err})")
                continue

        # PARA 이동 (첨부+노트, frontmatter sources 갱신)
        para = _normalize_para_coord(item.get("proposed_para_path") or "")
        if para:
            ok, err = _relocate_to_para(item, para)
            if not ok:
                errors.append(f"{short} (PARA 이동 실패: {err})")
                continue
        # para 비면 staging 잔류 — para_review:pending + 주간 §11.4 감사가 배치

        # commit point (strictly 마지막)
        ok, err = commit_fn(tid, label)
        (done if ok else errors).append(
            short if ok else f"{short} (commit 실패: {err})")

    if dry_run:
        if not planned:
            return (f"[{tag} 드레인 — dry-run] 처리 대상 없음\n", "")
        return (f"[{tag} 드레인 — dry-run] " + str(len(planned)) + "건 계획\n"
                + "\n".join(planned) + "\n", "")

    if not (done or repaired or errors):
        return ("", "")
    lines = [f"[{tag} → 9 완료] 자동 처리"]
    if done:
        lines.append(f"  ✓ 완료 {len(done)}건: " + ", ".join(done))
    if repaired:
        lines.append(f"  ↻ 라벨 복구 {len(repaired)}건: " + ", ".join(repaired))
    if errors:
        lines.append(f"  ✗ 실패 {len(errors)}건: " + "; ".join(errors))
    lines.append("")
    full = "\n".join(lines)
    # problem = 오류만 (성공 done/repaired 는 Telegram 침묵)
    prob: list[str] = []
    if errors:
        prob.append(f"[{tag}] ✗ 실패 {len(errors)}건: " + "; ".join(errors))
    problem = ("\n".join(prob) + "\n") if prob else ""
    return (full, problem)


def _run_save_drain(state: dict, now: dt.datetime, *,
                    dry_run: bool = False,
                    limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`1 저장` (={}) — audit 동반 노트만, 추가 액션 없음 (§11.3)."""
    return _run_label_drain(state, now, label=LABEL_SAVE, tag="1 저장",
                            extra_action=None, dry_run=dry_run, limit=limit)


def _schedule_extra_action(item: dict, now: dt.datetime) -> tuple[bool, str]:
    """`2 일정` 추가 액션 — 노트에서 일시 추출 → Google Calendar 이벤트 생성.
    **idempotent**: 노트 frontmatter 에 calendar_event_id 가 이미 있으면
    재생성 안 함 (크래시 후 재개 시 중복 이벤트 방지)."""
    note_rel = item.get("note_path")
    if note_rel:
        p = VAULT_ROOT / note_rel
        if p.exists():
            try:
                fm = _parse_frontmatter(p.read_text(encoding="utf-8"))
                if fm.get("calendar_event_id"):
                    return (True, "")          # 이미 생성됨 (재개 경로)
            except OSError:
                pass
    ev = _extract_schedule_from_email(item, now)
    if ev is None:
        return (False, "일정 추출 실패 — 메일에 명확한 일시 없음 (수동 처리 필요)")
    event_id, cerr = _create_calendar_event(
        ev, item.get("msg_id", ""), item.get("note_path", ""))
    if not event_id:
        return (False, f"Calendar 생성 실패: {cerr}")
    _attach_schedule_to_note(item, event_id, ev)
    return (True, "")


def _run_schedule_drain(state: dict, now: dt.datetime, *,
                        dry_run: bool = False,
                        limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`2 일정` (={일정}) — audit 노트 + Google Calendar 이벤트 + 9 완료 (§11.5)."""
    return _run_label_drain(state, now, label=LABEL_SCHEDULE, tag="2 일정",
                            extra_action=_schedule_extra_action,
                            dry_run=dry_run, limit=limit)


def _task_extra_action(item: dict, now: dt.datetime,
                       state: dict) -> tuple[bool, str]:
    """`6 할일` 추가 액션 — 노트에서 할 일 추출 → Google Tasks 등록.
    **idempotent**: 노트 frontmatter 에 google_task_id 가 이미 있으면
    재생성 안 함 (크래시 후 재개 시 중복 task 방지). schedule 핸들러의
    calendar_event_id 가드와 평행.
    일정과 달리 추출이 실패해도 제목 fallback 으로 항상 task 1개 생성 —
    `6 할일` 라벨 자체가 Dr. Ben 의 '이건 할 일' 결정이므로 commit 막지 않음."""
    note_rel = item.get("note_path")
    if note_rel:
        p = VAULT_ROOT / note_rel
        if p.exists():
            try:
                fm = _parse_frontmatter(p.read_text(encoding="utf-8"))
                if fm.get("google_task_id"):
                    return (True, "")          # 이미 생성됨 (재개 경로)
            except OSError:
                pass
    te = _extract_task_from_email(item, now)
    tid = item.get("msg_id", "")
    notes = (f"Gmail thread: https://mail.google.com/mail/u/0/#all/{tid}\n"
             f"Vault note: {note_rel or '(노트 없음)'}")
    if te.get("detail"):
        notes = te["detail"] + "\n\n" + notes
    task_id = create_gtask(state, te["title"], notes, te.get("due") or None)
    if not task_id:
        return (False, "Google Tasks 생성 실패 (gog OAuth/네트워크?)")
    _attach_task_to_note(item, task_id, te)
    return (True, "")


def _run_task_drain(state: dict, now: dt.datetime, *,
                    dry_run: bool = False,
                    limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`6 할일` (={할일}) — audit 노트 + Google Tasks + 9 완료 (§11.5).
    extra_action 은 state(gtasks_list_id 캐시·자동생성) 가 필요해 closure 로
    바인딩 — `_run_label_drain` 의 (item, now) 콜백 계약은 그대로 유지."""
    return _run_label_drain(
        state, now, label=LABEL_TASK, tag="6 할일",
        extra_action=lambda it, n: _task_extra_action(it, n, state),
        dry_run=dry_run, limit=limit)


def _sched_task_extra_action(item: dict, now: dt.datetime,
                             state: dict) -> tuple[bool, str]:
    """`3 일정+할일` 추가 액션 — `6 할일`·`2 일정` atomic 합성 (순수 terminal).
    **새 추출/등록 로직 없음** — `_task_extra_action`+`_schedule_extra_action`
    조합만. 둘 다 멱등(google_task_id / calendar_event_id frontmatter 가드)
    이라 합성도 자동 멱등(재개·액션-인지 가드서 각자 self-skip/보강).
    순서 = **task 먼저**: task 는 항상 성공(제목 fallback)이라 먼저 확정해
    두면, 일시 추출 실패로 schedule 이 commit 보류(=`2 일정` 계약)돼도 할일
    절반은 유실 안 됨. 재개 시 task 는 마커로 self-skip, schedule 만 재시도.
    schedule 실패 → (False,err) 전파: commit 차단 + 오류 발화(`2 일정` 와
    동일 — 메일에 명확한 일시 없으면 수동 처리 필요)."""
    ok, err = _task_extra_action(item, now, state)
    if not ok:
        return (False, f"할일 부분 실패: {err}")
    ok, err = _schedule_extra_action(item, now)
    if not ok:
        return (False, f"일정 부분 실패: {err}")
    return (True, "")


def _run_sched_task_drain(state: dict, now: dt.datetime, *,
                          dry_run: bool = False,
                          limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`3 일정+할일` (={일정,할일}) — Google Tasks + Calendar 이벤트 + 9 완료
    (§11.5). 회신 미포함 → **순수 terminal**: commit_fn 기본
    `_commit_action_label`(라벨 제거 + `9 완료`). extra_action 이 state(task
    gtasks_list_id 캐시) 필요해 closure 바인딩(`_run_task_drain` 패턴)."""
    return _run_label_drain(
        state, now, label=LABEL_SCHED_TASK, tag="3 일정+할일",
        extra_action=lambda it, n: _sched_task_extra_action(it, n, state),
        dry_run=dry_run, limit=limit)


def _commit_reply_label(thread_id: str, src_label: str) -> tuple[bool, str]:
    """`8 회신` phase-1 커밋 — **archive 만, 라벨 유지** (비-terminal).
    `9 완료` 안 박고 `8 회신` 도 안 떼어냄: 발송 전엔 미완. INBOX 만 제거해
    받은편지함에서 내림. `8 회신` 은 *persistent 중간상태 마커* 로 남아
    crash 복구(state 유실 시 재진입 → `gmail_draft_id` 가드로 멱등) 보장.
    phase-2 = `_poll_awaiting_replies` 가 발송 감지 후 `9 완료` promote +
    `8 회신` 제거. (드레인 재처리는 awaiting_reply skip 필터가 차단.)"""
    return gog_call("gmail", "labels", "modify", thread_id, "--remove", "INBOX")


def _find_existing_draft_for_thread(thread_id: str) -> "str | None":
    """스레드에 이미 만들어진 초안의 draftId 조회 (Gmail-레벨 멱등 안전망).
    frontmatter 마커가 못 쓰인 창(초안 생성 성공 후 마커 기록 전 크래시/
    에러)에서 재시도 시 **중복 초안** 방지 — `calendar_event_id`/
    `google_task_id` 와 같은 외부-부작용 멱등성 원칙. gog drafts list 에
    threadId 필터가 없어 전체 조회 후 message.threadId 매칭. 없으면 None."""
    out = gog_json("gmail", "drafts", "list")
    if isinstance(out, dict):
        out = out.get("drafts", out.get("items", []))
    for d in out or []:
        if not isinstance(d, dict):
            continue
        # gog drafts list 실측 구조 = flat {id, messageId, threadId}
        # (2026-05-17). 중첩 message.threadId 는 다른 gog 버전 대비 폴백.
        tid = d.get("threadId") or (d.get("message") or {}).get("threadId")
        if tid == thread_id:
            return d.get("id") or d.get("draftId")
    return None


def _draft_id_from_create(out_json) -> str:
    """gog `drafts create` 응답에서 draftId 추출. 실제 응답 형태(2026-05-17
    실측): {'draftId': 'r…', 'message': {'id':…,'threadId':…}, 'threadId':…}.
    send/delete 가 쓰는 정본 = top-level `draftId`. 폴백: message.id → id."""
    if not isinstance(out_json, dict):
        return ""
    return (out_json.get("draftId")
            or (out_json.get("message") or {}).get("id")
            or out_json.get("id") or "")


def _reply_extra_action(item: dict, now: dt.datetime, state: dict,
                        drafted: list,
                        src_label: str = LABEL_REPLY) -> tuple[bool, str]:
    """회신 초안 추론 → Gmail Drafts → awaiting_reply 등록 (`8 회신` 및 회신
    포함 복합 `4·5·7` 공용). 무인 회신 초안 파이프라인(구 인터랙티브 답장
    경로의 후신 — 2026-05-17 레거시 삭제로 이제 유일 회신 경로).
    **idempotent**: 노트 frontmatter `gmail_draft_id` 있으면 재초안 안 함
    (awaiting_reply 엔트리만 보강해 crash 복구).
    src_label: 트리거 라벨 — awaiting_reply 엔트리에 기록해 `_poll_awaiting_
    replies` 가 발송감지 시 *그 라벨*(`8 회신`/`4 일정+회신`/…)을 떼고
    `9 완료` 부착하게 함. 복합서 `8 회신` 하드코딩이면 엉뚱한 라벨 제거됨.
    drafted: 이번 사이클 *신규* 초안 short 제목 누적 (§11.5 silent-on-
    success 예외 — 사이클당 1건 요약용; 멱등 skip 분은 미누적)."""
    note_rel = item.get("note_path")
    thread_id = item.get("msg_id", "")
    if not note_rel or not thread_id:
        return (False, "노트 경로/스레드 ID 없음 — 초안 보류")
    note_path = VAULT_ROOT / note_rel

    # ── 멱등 가드 (2단: frontmatter 마커 → Gmail drafts 조회) ──
    existing_draft = ""
    if note_path.exists():
        try:
            fm = _parse_frontmatter(note_path.read_text(encoding="utf-8"))
            existing_draft = (fm.get("gmail_draft_id") or "").strip()
        except OSError:
            pass
    if not existing_draft:
        # 마커 없음 — 초안 생성 성공 후 마커 기록 전 죽었을 수 있음.
        # Gmail 에 그 스레드 초안이 이미 있으면 재사용(중복 생성 차단,
        # 직전 run 의 고아 초안 자동 흡수). 마커도 보강 기록.
        gid = _find_existing_draft_for_thread(thread_id)
        if gid:
            existing_draft = gid
            _attach_draft_to_note(item, gid)
    if existing_draft:
        q = state.setdefault("awaiting_reply", [])
        if not any(e.get("msg_id") == thread_id for e in q):
            # state 유실 복구: drafted_at 생략 → _poll 가 drafted_ms=0 으로
            # 처리(스레드에 SENT 있으면 즉시 promote — 이미 보냈을 수 있음).
            q.append({
                "msg_id": thread_id, "drafted_at": "",
                "draft_id": existing_draft,
                "subject": item.get("subject", ""),
                "vault_note_path": note_rel,
                "src_label": src_label, "terminal": LABEL_DONE_9,
            })
        return (True, "")          # 재공지 안 함 (drafted 미누적)

    # ── 신규 초안 작성 ──
    payload = fetch_thread_full(thread_id, out_dir=None)
    if payload is None:
        return (False, "thread fetch 실패")
    msg = _pick_target_message(payload, thread_id)
    if not msg:
        return (False, "타깃 메시지 추출 실패")
    headers = _headers_to_dict(msg.get("payload", {}).get("headers", []))
    frm = headers.get("from", "")
    subject = headers.get("subject", "(제목 없음)")
    reply_to = headers.get("reply-to", "") or frm
    body_text = _extract_plain_text(msg.get("payload") or {}) or msg.get("snippet", "")
    vault_ctx = _search_vault_context(item)
    prior_threads = _search_prior_threads(item)

    prompt_parts = [
        "다음 Gmail 메일에 대한 한국어 회신 초안을 작성하라.",
        "Dr. Ben(benkorea.ai@gmail.com)이 보낼 회신이며, 정중한 존댓말을 쓴다.",
        "**무인 자동 초안** — Dr. Ben 이 Gmail Drafts 에서 검토 후 직접 발송한다."
        " 사실관계를 임의 단정하지 말 것.",
        "",
        "출력 형식 (엄격):",
        "- 첫 줄에 정확히 `STATUS: ok` 또는 `STATUS: review` 중 하나.",
        "  · ok = 회신 의도·필요 정보가 명확, 그대로 보낼 만한 초안.",
        "  · review = 의도 불분명/가정 과다/Dr. Ben 의 사실 확인 필요."
        " 이 경우 본문은 단정 대신 정중한 확인·명확화 요청 형태로.",
        "- 둘째 줄부터 회신 본문만 (인사·서명 가능, 코드블록·메타 설명 금지).",
        "",
        f"[원본 메일 from: {frm}]",
        f"[subject: {subject}]",
        "",
        "[원본 본문]",
        body_text[:4000],
        "",
    ]
    if prior_threads:
        prompt_parts += ["[같은 발신자의 직전 thread]", prior_threads, ""]
    if vault_ctx:
        prompt_parts += ["[vault 관련 노트 발췌 — 톤·맥락 참고]", vault_ctx, ""]
    prompt = "\n".join(prompt_parts)

    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-opus-4-7",
        "--disallowedTools",
        "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True,
                           text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return (False, "LLM timeout")
    if r.returncode != 0:
        return (False, f"LLM exit={r.returncode}")
    out = (r.stdout or "").strip()
    if out.startswith("```"):
        nl = out.find("\n")
        out = out[nl + 1:] if nl >= 0 else out[3:]
        if out.rstrip().endswith("```"):
            out = out.rstrip()[:-3]
        out = out.strip()
    # STATUS 라인 파싱·제거 (이메일 본문엔 절대 안 들어가게)
    review = True               # 보수적 기본 — STATUS 누락 시 검토 플래그
    lines = out.split("\n", 1)
    if lines and lines[0].strip().upper().startswith("STATUS:"):
        token = lines[0].split(":", 1)[1].strip().lower()
        review = (token != "ok")
        out = lines[1].strip() if len(lines) > 1 else ""
    body = out.strip()
    if not body:
        return (False, "LLM 빈 응답")

    re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    fd_b, body_file = tempfile.mkstemp(prefix=".gws-reply.", suffix=".txt")
    try:
        with os.fdopen(fd_b, "w", encoding="utf-8") as f:
            f.write(body)
        out_json = gog_json(
            "gmail", "drafts", "create",
            "--to", reply_to, "--subject", re_subject,
            "--body-file", body_file,
            "--reply-to-message-id", thread_id,
        )
    finally:
        try:
            os.unlink(body_file)
        except OSError:
            pass
    if out_json is None or out_json == []:
        return (False, "gog drafts create 실패")
    draft_id = _draft_id_from_create(out_json)
    if not draft_id:
        # 초안이 실제로 생겼을 수도(파싱만 실패) → Gmail 조회로 회수,
        # 중복 생성 금지. 그래도 없으면 진짜 실패.
        draft_id = _find_existing_draft_for_thread(thread_id) or ""
    if not draft_id:
        return (False, f"drafts create 응답 비정상: {out_json}")

    _attach_draft_to_note(item, draft_id)
    # reply_review / brainify_origin 마커 (best-effort 2차 atomic write)
    if note_path.exists():
        try:
            t = note_path.read_text(encoding="utf-8")
            t = _replace_fm_field(t, "reply_review",
                                  "needed" if review else "pending")
            t = _replace_fm_field(t, "brainify_origin", "reply-label")
            fd, tmp = tempfile.mkstemp(prefix=".gws-note.",
                                       dir=str(note_path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(t)
            os.replace(tmp, note_path)
        except OSError:
            try:
                os.unlink(tmp)
            except (OSError, NameError):
                pass

    state.setdefault("awaiting_reply", []).append({
        "msg_id": thread_id,
        "drafted_at": now.isoformat(timespec="seconds"),
        "draft_id": draft_id,
        "subject": subject,
        "vault_note_path": note_rel,
        "src_label": src_label, "terminal": LABEL_DONE_9,
    })
    drafted.append(_short_subject(subject, 40)
                   + (" [검토필요]" if review else ""))
    return (True, "")


def _run_reply_composite_drain(state: dict, now: dt.datetime, *,
                               label: str, tag: str, pre=None,
                               dry_run: bool = False,
                               limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """회신 포함 라벨(`8`·`4`·`5`·`7`) 공용 2단계 비-terminal 러너.
    pre: 회신 *전* 실행할 비-회신 atomic 합성 (item,now)->(ok,err).
         실패 시 bail(commit 차단+발화) — 초안·awaiting_reply 생성 *전* 이라
         고아 초안 없음. None 이면 순수 `8 회신`. reply 는 항상 마지막.
    commit_fn=`_commit_reply_label`(비-terminal·라벨 유지). 발송감지→
    `9 완료` promote 는 `_poll_awaiting_replies`(awaiting_reply src_label=
    label 이라 *그 라벨*을 떼고 `9 완료`). sent-poll 조율: 동반노트
    `gmail_threadIds` canonical → sent-poll threadId 가드가 이미-노트화 skip.
    Telegram: silent-on-success 의 §11.5 명문화 예외 — 이번 사이클 *신규*
    초안만 1건 요약으로 problem 채널에 합성(cmd_poll 이 Telegram 전달)."""
    drafted: list[str] = []

    def _xa(it, n):
        if pre is not None:
            ok, err = pre(it, n)
            if not ok:
                return (False, err)
        return _reply_extra_action(it, n, state, drafted, src_label=label)

    full, problem = _run_label_drain(
        state, now, label=label, tag=tag,
        extra_action=_xa, commit_fn=_commit_reply_label,
        dry_run=dry_run, limit=limit)
    if drafted and not dry_run:
        summary = (f"[{tag} → 검토대기] 초안 {len(drafted)}건 — "
                   f"Gmail Drafts 검토·발송 요망: " + ", ".join(drafted) + "\n")
        full = (full + summary) if full else summary
        problem = (problem + summary) if problem else summary
    return (full, problem)


def _run_reply_label_drain(state: dict, now: dt.datetime, *,
                           dry_run: bool = False,
                           limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`8 회신` (={회신}) — pre 없는 순수 회신 (§11.5 spec-lock)."""
    return _run_reply_composite_drain(
        state, now, label=LABEL_REPLY, tag="8 회신", pre=None,
        dry_run=dry_run, limit=limit)


def _run_sched_reply_drain(state: dict, now: dt.datetime, *,
                           dry_run: bool = False,
                           limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`4 일정+회신` (={일정,회신}) — Calendar 이벤트 + 회신 초안 + 2단계.
    pre=schedule: 일시 추출 실패 시 commit 보류+발화(`2 일정` 계약) — reply
    생성 전 bail 이라 고아 초안 없음. 재개 시 schedule 만 재시도 후 reply."""
    return _run_reply_composite_drain(
        state, now, label=LABEL_SCHED_REPLY, tag="4 일정+회신",
        pre=lambda it, n: _schedule_extra_action(it, n),
        dry_run=dry_run, limit=limit)


def _run_task_reply_drain(state: dict, now: dt.datetime, *,
                          dry_run: bool = False,
                          limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`7 할일+회신` (={할일,회신}) — Google Task + 회신 초안 + 2단계.
    pre=task(항상 성공·멱등)."""
    return _run_reply_composite_drain(
        state, now, label=LABEL_TASK_REPLY, tag="7 할일+회신",
        pre=lambda it, n: _task_extra_action(it, n, state),
        dry_run=dry_run, limit=limit)


def _run_sched_task_reply_drain(state: dict, now: dt.datetime, *,
                                dry_run: bool = False,
                                limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """`5 일정+할일+회신` (={일정,할일,회신}) — Task + Calendar + 회신 초안
    + 2단계. pre=`_sched_task_extra_action`(=`3` 의 task→schedule, bail 포함)
    그대로 재사용 — 복합의 복합도 순수 조합."""
    return _run_reply_composite_drain(
        state, now, label=LABEL_SCHED_TASK_REPLY, tag="5 일정+할일+회신",
        pre=lambda it, n: _sched_task_extra_action(it, n, state),
        dry_run=dry_run, limit=limit)


def _run_reply_drain(state: dict, now: dt.datetime, *,
                     dry_run: bool = False,
                     limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """보낸편지함 전체(회신+콜드)를 브레인화 (§11.5, 라벨 0마찰 경로).
    2026-05-16 Dr. Ben: 방향 무관 — 내가 보낸 메일은 회신이든 내가 먼저
    시작한 콜드메일이든 audit 가치 있으면 노트화(가치 판단은 노트 LLM 단계).
    - 라벨 없는 모델: 제거할 라벨도 `9 완료` 부착도 없음(그건 1~8 액션 터미널).
      멱등성 = **스레드 노트 존재**(threadId 가드)뿐.
    - KIRAMS 포워딩 노이즈 배제 = `[KIRAMS-FWD]` 제목(항상 노이즈) +
      `-(from:kirams.re.kr subject:"[FW]")`(prefix 없는 변종). archive 후엔
      라벨모양이 진짜 보낸 메일과 같아 `-in:inbox` 불가; Dr. Ben 이 KIRAMS
      별칭 send-as 진짜 메일도 보내므로 from:kirams 전체 배제도 불가 →
      from:kirams **AND [FW]** 조합 정밀 배제(별칭의 진짜 Re:/신규 보존).
    - 노트는 *보낸 메시지*(회신이면 원문 인용 포함)로 빌드해 교신 캡처. 단
      frontmatter gmail_threadIds 는 canonical threadId 로 덮어써 가드 멱등 일치.
    v1 한계: 노트 있는 스레드의 *추가* 메일 미포착 (thread 진화 append 는
    gmail-capture §9.2 로 위임). 라벨핸들러와의 교차 중복은 드물고 주간 §9.2
    감사가 포착. 반환 (full, problem) — 성공 침묵·오류만."""
    # KIRAMS 포워딩 노이즈 정밀 배제 (2026-05-16 2차 dry-run 으로 확정):
    #  - 이미 archive 돼 `-in:inbox` 로는 진짜 보낸 메일과 구분 불가.
    #  - Dr. Ben 은 KIRAMS 별칭 send-as 로 진짜 회신/메일도 Gmail 에서
    #    보냄 → from:kirams 전체 배제 불가(정당분까지 막힘).
    #  - 정밀 구분: ① `[KIRAMS-FWD]` 제목 = 항상 노이즈(webmail-watch)
    #    ② from:kirams **AND** `[FW]` = prefix 없는 포워딩 변종.
    #  KIRAMS 별칭의 진짜 `Re:`/신규 메일은 [FW] 아니라 보존됨.
    query = (f'in:sent -subject:"[KIRAMS-FWD]" '
             f'-(from:kirams.re.kr subject:"[FW]") '
             f'-label:"{LABEL_DONE_9}" newer_than:{REPLY_SENT_WINDOW_DAYS}d')
    results = gog_json("gmail", "search", query, "--max", str(limit))
    if results is None:
        _m = "[보낸메일 드레인] gmail 검색 실패 (gog OAuth?)\n"
        return (_m, _m)
    if isinstance(results, dict):
        results = results.get("messages", results.get("items", []))
    if not results:
        # 수동 dry-run 은 0건도 명시 (cron 비-dry 는 침묵 유지)
        if dry_run:
            return ("[보낸메일 드레인 — dry-run] 처리 대상 없음 (검색 0건)\n", "")
        return ("", "")

    done: list[str] = []
    errors: list[str] = []
    planned: list[str] = []

    for m in results:
        tid = m.get("id")    # gog 검색은 thread 단위 — id == threadId
        subj = (m.get("subject") or "").strip() or "(제목 없음)"
        short = _short_subject(subj, 50)
        if not tid:
            continue

        # tick 예산 게이트 (FailoverError 방어) — 소진 시 잔여는 다음
        # tick 멱등 재개 (스레드 노트 존재 가드가 done 마커).
        if not dry_run and not _tick_budget_left():
            break

        # 멱등: 이 스레드 노트 이미 있으면 skip (라벨 없는 모델의 done 마커)
        existing, _ = _existing_note_for_thread(tid)
        if existing is not None:
            continue

        # 2026-05-16 Dr. Ben: 회신+콜드 전부 (cold 제외 없음).
        if dry_run:
            planned.append(f"· {short} → [보낸메일 브레인화]")
            continue

        # 노트: 스레드의 *최신 SENT 메시지*(= 내가 보낸 회신/메일)로 빌드.
        # target_label="SENT" 가 multi-msg 스레드에서 정확한 메시지 선택
        # (이전 버그: msgs[0]=원본 inbound 로 잘못 생성됐었음).
        item: dict = {"msg_id": tid, "from": m.get("from", ""),
                      "subject": subj}
        _spend_tick_budget()   # 무거운 sent-poll 1건 소비
        ok, err = propose_proceed(item, target_label="SENT")
        if not ok:
            errors.append(f"{short} ({err})")
            continue
        note_rel = item.get("note_path")

        # 첨부 파서 dispatch → provenance (블록은 _run_label_drain 신규경로
        # 미러 — 향후 분해 단위에서 DRY 통합, 검증된 코어 재흔들기 회피).
        parsed: list[tuple[str, dict]] = []
        all_warns: list[str] = []
        for arel in item.get("attachments") or []:
            ap = VAULT_ROOT / arel
            out = parse_attachment(ap)
            parsed.append((_strip_gog_prefix(ap.name), out))
            all_warns.extend(out.get("warnings") or [])
        if note_rel:
            _save_append_parsed(note_rel, parsed)
            _save_inject_provenance(note_rel, "internal",
                                    PARSER_VERSION, all_warns)
            # gmail_threadIds 를 canonical threadId 로 덮어써 가드 멱등 일치
            # + 보낸메일 출처 마커 (라벨 없으니 노트가 유일 기록)
            p = VAULT_ROOT / note_rel
            if p.exists():
                try:
                    t = p.read_text(encoding="utf-8")
                    t = _replace_fm_field_list(t, "gmail_threadIds", [tid])
                    t = _replace_fm_field(t, "brainify_origin", "gmail-sent")
                    t = _replace_fm_field(
                        t, "sent_brainified_at",
                        now.isoformat(timespec="seconds"))
                    fd, tmp = tempfile.mkstemp(prefix=".gws-note.",
                                               dir=str(p.parent))
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(t)
                    os.replace(tmp, p)
                except OSError:
                    try:
                        os.unlink(tmp)
                    except (OSError, NameError):
                        pass

        # PARA 이동 (라벨 commit 없음 — 노트 존재가 done 마커)
        para = _normalize_para_coord(item.get("proposed_para_path") or "")
        if para:
            ok, err = _relocate_to_para(item, para)
            if not ok:
                errors.append(f"{short} (PARA 이동 실패: {err})")
                continue
        done.append(short)

    if dry_run:
        if not planned:
            return ("[보낸메일 드레인 — dry-run] 처리 대상 없음\n", "")
        return ("[보낸메일 드레인 — dry-run] " + str(len(planned)) + "건 계획\n"
                + "\n".join(planned) + "\n", "")
    if not (done or errors):
        return ("", "")
    lines = ["[보낸메일 브레인화] 자동 처리"]
    if done:
        lines.append(f"  ✓ 완료 {len(done)}건: " + ", ".join(done))
    if errors:
        lines.append(f"  ✗ 실패 {len(errors)}건: " + "; ".join(errors))
    lines.append("")
    full = "\n".join(lines)
    prob = (f"[보낸메일] ✗ 실패 {len(errors)}건: " + "; ".join(errors) + "\n"
            if errors else "")
    return (full, prob)


def cmd_save_drain(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """`/gws-assistant save-drain [--dry-run] [N]` — `1 저장` 수동 드레인.
    cron 자동발화와 동일 로직, 검증·수동 트리거용."""
    dry = "--dry-run" in argv
    lim = GMAIL_SAVE_MAX
    for a in argv:
        if a.isdigit():
            lim = max(1, int(a))
    full, _problem = _run_save_drain(state, now, dry_run=dry, limit=lim)
    if full:
        print(full, end="" if full.endswith("\n") else "\n")
    save_state(state)
    return 0


def cmd_schedule_drain(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """`/gws-assistant schedule-drain [--dry-run] [N]` — `2 일정` 수동 드레인.
    cron 자동발화와 동일 로직 (Calendar 이벤트 + 노트 + 9 완료), 검증·수동용."""
    dry = "--dry-run" in argv
    lim = GMAIL_SAVE_MAX
    for a in argv:
        if a.isdigit():
            lim = max(1, int(a))
    full, _problem = _run_schedule_drain(state, now, dry_run=dry, limit=lim)
    if full:
        print(full, end="" if full.endswith("\n") else "\n")
    save_state(state)
    return 0


def cmd_task_drain(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """`/gws-assistant task-drain [--dry-run] [N]` — `6 할일` 수동 드레인.
    cron 자동발화와 동일 로직 (Google Tasks + 노트 + 9 완료), 검증·수동용."""
    dry = "--dry-run" in argv
    lim = GMAIL_SAVE_MAX
    for a in argv:
        if a.isdigit():
            lim = max(1, int(a))
    full, _problem = _run_task_drain(state, now, dry_run=dry, limit=lim)
    if full:
        print(full, end="" if full.endswith("\n") else "\n")
    save_state(state)
    return 0


def cmd_schedule_task_drain(state: dict, now: dt.datetime,
                            argv: list[str]) -> int:
    """`/gws-assistant schedule-task-drain [--dry-run] [N]` — `3 일정+할일`
    수동 드레인. cron 과 동일 (Tasks + Calendar + 9 완료), 검증·수동용."""
    dry = "--dry-run" in argv
    lim = GMAIL_SAVE_MAX
    for a in argv:
        if a.isdigit():
            lim = max(1, int(a))
    full, _problem = _run_sched_task_drain(state, now, dry_run=dry, limit=lim)
    if full:
        print(full, end="" if full.endswith("\n") else "\n")
    save_state(state)
    return 0


def _cmd_simple_drain(state: dict, now: dt.datetime, argv: list[str],
                      runner) -> int:
    """수동 드레인 공용 (--dry-run/[N] 파싱 + 출력 + save). 검증·수동용."""
    dry = "--dry-run" in argv
    lim = GMAIL_SAVE_MAX
    for a in argv:
        if a.isdigit():
            lim = max(1, int(a))
    full, _problem = runner(state, now, dry_run=dry, limit=lim)
    if full:
        print(full, end="" if full.endswith("\n") else "\n")
    save_state(state)
    return 0


def cmd_sched_reply_drain(state, now, argv):
    """`schedule-reply-drain` — `4 일정+회신` 수동 드레인."""
    return _cmd_simple_drain(state, now, argv, _run_sched_reply_drain)


def cmd_task_reply_drain(state, now, argv):
    """`task-reply-drain` — `7 할일+회신` 수동 드레인."""
    return _cmd_simple_drain(state, now, argv, _run_task_reply_drain)


def cmd_sched_task_reply_drain(state, now, argv):
    """`schedule-task-reply-drain` — `5 일정+할일+회신` 수동 드레인."""
    return _cmd_simple_drain(state, now, argv, _run_sched_task_reply_drain)


def cmd_reply_drain(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """`/gws-assistant reply-drain [--dry-run] [N]` — 보낸편지함 회신 브레인화
    수동 트리거. cron 자동발화와 동일 로직, 검증·수동용."""
    dry = "--dry-run" in argv
    lim = GMAIL_SAVE_MAX
    for a in argv:
        if a.isdigit():
            lim = max(1, int(a))
    full, _problem = _run_reply_drain(state, now, dry_run=dry, limit=lim)
    if full:
        print(full, end="" if full.endswith("\n") else "\n")
    save_state(state)
    return 0


def cmd_reply_label_drain(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """`/gws-assistant reply-label-drain [--dry-run] [N]` — `8 회신` 수동 드레인.
    cron 자동발화와 동일 로직 (초안 작성 + awaiting_reply 2단계 + 사이클당
    1건 요약), 검증·수동용. `reply-drain`(보낸편지함 sent-poll)과 별개."""
    dry = "--dry-run" in argv
    lim = GMAIL_SAVE_MAX
    for a in argv:
        if a.isdigit():
            lim = max(1, int(a))
    full, _problem = _run_reply_label_drain(state, now, dry_run=dry, limit=lim)
    if full:
        print(full, end="" if full.endswith("\n") else "\n")
    save_state(state)
    return 0


def cmd_poll(state: dict, now: dt.datetime, force: bool) -> int:
    """§11 무인 폴 (구 3-라벨 classify→plan→approve 분기 2026-05-17 레거시 삭제):
       1. awaiting_reply 발송 감지 → `9 완료` promote (회신 2단계 완결).
       2. autodrain 활성 시 1~8 라벨 + sent-poll 일괄 드레인.
       둘 다 게이트 무관 (백그라운드). force 인자는 호출 계약 유지용(무효과)."""
    _arm_tick_budget()   # cron poll 만 유한 tick 예산 (FailoverError 방어)
    awaiting_msg = _poll_awaiting_replies(state, now)

    # §11 액션-라벨 완전무인 드레인 — 게이트 무관 (백그라운드).
    # 단일 킬스위치 state['autodrain_enabled'] (기본 False) 가 1~8 핸들러
    # 전부를 관장 (2026-05-16 Dr. Ben: 라벨별 플래그 대신 단일 — 인지부하 최소·현실 부합).
    # Telegram: 성공 완전 침묵, 오류 시에만 발화 (일일 다이제스트 없음).
    # 핸들러 계약: (state, now) -> (full, problem). 2~8 은 §11.5 에서
    # 아래 튜플에 append (동일 게이트·동일 problem 누적).
    if state.get("autodrain_enabled"):
        _problems: list[str] = []
        for _handler in (_run_save_drain, _run_schedule_drain, _run_task_drain, _run_sched_task_drain, _run_reply_label_drain, _run_sched_reply_drain, _run_task_reply_drain, _run_sched_task_reply_drain, _run_reply_drain):  # 1·2·6·3·8·4·7·5·sent-poll (전 라벨 + 보낸편지함).
            _full, _problem = _handler(state, now)
            if _problem:
                _problems.append(_problem)
        if _problems:
            _pm = "".join(_problems)
            awaiting_msg = (_pm + awaiting_msg) if awaiting_msg else _pm

    if awaiting_msg:
        print(awaiting_msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


# ============================================================================
# Main / arg parsing
# ============================================================================

# ============================================================================
# Correction log — 사용자 피드백을 vault gmail-capture.md 에 누적,
# 다음 LLM 분류 시 프롬프트로 자동 주입되어 자기진화.
# ============================================================================


_LEARN_RULES_LINE_RE = re.compile(
    r"^- \d{4}-\d{2}-\d{2}: from=(.+?),\s+subject=\"[^\"]*\":\s*[^→]*→(\w+)"
)
_EMAIL_ADDR_RE = re.compile(r"<([^>]+)>")


LEGACY_CMDS = frozenset({
    "approve", "cancel", "correct", "reclassify", "learn-rules", "show-rules",
    "snooze", "confirm", "edit", "skip", "dismiss", "draft-reply", "reply",
    "reply-task", "gtask", "schedule", "nl", "migrate-inbox",
    "migrate-brainify-labels", "pending-review", "bulk-reclassify",
})


def main(argv: list[str]) -> int:
    state = load_state()
    now = now_kst()

    # subcommand parsing
    if argv:
        first = argv[0]
        if first in LEGACY_CMDS:
            # 구 3-라벨 classify→plan→approve / `/g` 인터랙티브 모델은
            # §11 8-라벨 인간-스와이프 모델로 폐기됨 (2026-05-17 레거시 삭제).
            print(f"[gws-assistant] '{first}' 은 폐기된 레거시 명령입니다 "
                  f"(§11 8-라벨 모델 이행, 2026-05-17). 폰 스와이프로 액션 "
                  f"라벨을 붙이고 `*-drain` 서브커맨드를 사용하세요.",
                  file=sys.stderr)
            return 1
        if first == "status":
            return cmd_status(state, now)
        if first == "save-drain":
            return cmd_save_drain(state, now, argv[1:])
        if first == "schedule-drain":
            return cmd_schedule_drain(state, now, argv[1:])
        if first == "task-drain":
            return cmd_task_drain(state, now, argv[1:])
        if first == "schedule-task-drain":
            return cmd_schedule_task_drain(state, now, argv[1:])
        if first == "reply-drain":
            return cmd_reply_drain(state, now, argv[1:])
        if first == "reply-label-drain":
            return cmd_reply_label_drain(state, now, argv[1:])
        if first == "schedule-reply-drain":
            return cmd_sched_reply_drain(state, now, argv[1:])
        if first == "task-reply-drain":
            return cmd_task_reply_drain(state, now, argv[1:])
        if first == "schedule-task-reply-drain":
            return cmd_sched_task_reply_drain(state, now, argv[1:])
        if first == "--force-poll" or first == "--force-batch":
            return cmd_poll(state, now, force=True)
        # unknown — fall through to poll
        print(f"[gws-assistant] 알 수 없는 인자 무시: {argv}", file=sys.stderr)

    return cmd_poll(state, now, force=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
