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

# 라벨 정책 — 3분류
# 라벨 = "브레인화 작업 진행 상태", 콘텐츠 카테고리가 아님.
LABEL_NOISE = "브레인화/불필요"
LABEL_PENDING = "브레인화/보류"
LABEL_PROCEED = "브레인화/진행"   # 노트 작성됨 + 후속 작업 (followups) 진행 중. 임시.
LABEL_DONE = "브레인화/완료"      # 후속 작업까지 모두 종결. 영구 보존 (terminal).

# Legacy 라벨 — 이미 부착된 메일은 그대로 두고 inbox 검색에서만 제외.
LEGACY_LABEL_DUPLICATE = "브레인화/중복"

# ── 8-라벨 액션 트리아지 모델 (gmail-capture.md §11, 2026-05-16) ──
# Dr. Ben 이 폰/PC 에서 직접 부착하는 액션 라벨. 본 스킬은 그 중 `1 저장`만
# 완전무인 처리한다 (§11.3). 2~8 은 deferred (§11.5).
LABEL_SAVE = "1 저장"        # {} 행동 없음 — audit 동반 노트 + archive
LABEL_DONE_9 = "9 완료"      # 터미널 표식 (기계 검증된 완료). legacy LABEL_DONE 와 무충돌.
LABEL_SCHEDULE = "2 일정"    # {일정} — audit 노트 + Google Calendar 이벤트 (§11.5)
GMAIL_SAVE_MAX = 8           # 1회 드레인 상한 (멱등 — 잔여분 다음 사이클)
# 파서 추상화 provenance (§11.3). provider 교체 시 _parser_* 가 자기 id/version 반환.
PARSER_VERSION = "1"

# LLM 분류는 3 카테고리로 출력:
#   noise   → 라벨 LABEL_NOISE   + archive
#   pending → 라벨 LABEL_PENDING (inbox 유지) — 외부 액션 대기 또는 분류 자신없음
#   proceed → 라벨 LABEL_PROCEED + archive
CATEGORY_ORDER = ["noise", "proceed", "pending"]
VALID_CATEGORIES = set(CATEGORY_ORDER)


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

def is_korean_holiday(date: dt.date):
    """진짜 공휴일만 True. Google '대한민국의 휴일' 캘린더는 비공휴일 기념일
    (어버이날·어린이날 외 기타 기념일 등) 도 포함하므로 다음 두 신호로 거름:
    - transparency == 'transparent' → 기념일/일정 차단 안 함
    - description 에 '기념일' 포함 → 기념일임을 명시
    """
    s = date.isoformat()
    items = gog_json("calendar", "events", HOLIDAY_CAL,
                     "--from", f"{s}T00:00:00+09:00",
                     "--to", f"{s}T23:59:59+09:00")
    if items is None:
        return None
    if isinstance(items, dict):
        items = items.get("items", [])
    for ev in items or []:
        if ev.get("transparency") == "transparent":
            continue
        if "기념일" in (ev.get("description") or ""):
            continue
        return True
    return False


def is_busy_now(now: dt.datetime, events: list) -> bool:
    """timed event 안 → busy. 단 summary 에 BUSY_EXCEPTIONS 포함 시 예외.
    all-day 이벤트는 무시."""
    for ev in events:
        s = ev.get("start") or {}
        e = ev.get("end") or {}
        if "dateTime" not in s:
            continue
        ed_str = e.get("dateTime")
        if not ed_str:
            continue
        sd = dt.datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00")).astimezone(KST)
        ed = dt.datetime.fromisoformat(ed_str.replace("Z", "+00:00")).astimezone(KST)
        if not (sd <= now < ed):
            continue
        summary = ev.get("summary", "") or ""
        if any(kw in summary for kw in BUSY_EXCEPTIONS):
            continue
        return True
    return False


def fetch_today_events(now: dt.datetime):
    today = now.date().isoformat()
    from_iso = f"{today}T00:00:00+09:00"
    to_iso = f"{today}T23:59:59+09:00"
    seen = {}
    for cal_id, _ in WATCH_CALENDARS:
        items = gog_json("calendar", "events", cal_id, "--from", from_iso, "--to", to_iso)
        if items is None:
            continue
        if isinstance(items, dict):
            items = items.get("items", [])
        for ev in items or []:
            key = ev.get("id")
            if key and key not in seen:
                seen[key] = ev
    return list(seen.values())


def check_gates(now: dt.datetime) -> str | None:
    """Return None if all gates pass, otherwise a short reason string."""
    if now.weekday() >= 5:
        return "주말"
    if is_korean_holiday(now.date()) is True:
        return "공휴일"
    if not (WORK_START <= now.time() < WORK_END):
        return "근무시간 외"
    events = fetch_today_events(now)
    if is_busy_now(now, events):
        return "미팅 중 (판독 예외 외)"
    return None


# ============================================================================
# Data fetch
# ============================================================================

def fetch_inbox_pending():
    """받은편지함에 있고 brainify 라벨(3-라벨 + legacy 2개) 어디에도 안 붙은 메일.
    읽음/안읽음 무관 — 사용자가 Gmail 에서 직접 읽었지만 라벨이 없는 메일도 포함하여
    blind spot 제거. Gmail 기본 정렬(최신순)을 그대로 사용."""
    query = ("in:inbox "
             f"-label:{LABEL_NOISE} -label:{LABEL_PENDING} -label:{LABEL_PROCEED} "
             f"-label:{LABEL_DONE} -label:{LEGACY_LABEL_DUPLICATE}")
    items = gog_json("gmail", "search", query, "--max", str(GMAIL_MAX))
    if items is None:
        return None
    if isinstance(items, dict):
        items = items.get("messages", items.get("items", []))
    return items or []


def fetch_pending_labeled(limit: int = GMAIL_MAX):
    """'브레인화/보류' 라벨 메일을 limit 건 가져온다 — pending-review 정리용.
    in:inbox / archive 무관 (라벨만 보면 됨)."""
    query = f"label:{LABEL_PENDING}"
    items = gog_json("gmail", "search", query, "--max", str(limit))
    if items is None:
        return None
    if isinstance(items, dict):
        items = items.get("messages", items.get("items", []))
    return items or []


# ============================================================================
# Vault guide loader
# ============================================================================

def _vault_classification_guide() -> str:
    """gmail-capture.md §1 메일 분류 체계 + §2 판단 기준 발췌.
    SSOT: vault 파일이 권위. 파일 없거나 추출 실패 시 빈 문자열(호출자가 fallback)."""
    try:
        text = VAULT_CAPTURE_DOC.read_text(encoding="utf-8")
    except OSError:
        return ""
    start = text.find("## 1. 메일 분류 체계")
    if start < 0:
        return ""
    end = text.find("## 3. ", start)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


# ============================================================================
# Classifier (heuristic)
# ============================================================================

def load_deterministic_rules() -> dict:
    """학습된 deterministic 분류 규칙 (cmd_learn_rules 가 누적)."""
    if not DETERMINISTIC_RULES_PATH.exists():
        return {}
    try:
        return json.loads(DETERMINISTIC_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_deterministic_rules(rules: dict) -> None:
    DETERMINISTIC_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    DETERMINISTIC_RULES_PATH.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def classify_email_heuristic(m: dict) -> tuple[str | None, str]:
    """학습된 deterministic 규칙 우선 + 하드코딩 noise 패턴 fallback.
    Returns (category, reason). category=None 이면 LLM 분류 필요."""
    frm_lower = (m.get("from") or "").lower()

    # 1. 학습된 deterministic 규칙 (cmd_learn_rules 가 채움)
    rules = load_deterministic_rules()
    for pattern, info in rules.items():
        if pattern.lower() in frm_lower:
            cat = info.get("category", "pending")
            if cat in VALID_CATEGORIES:
                return (cat, f"학습 규칙 '{pattern}' ({info.get('source_corrections', '?')}회 누적)")

    # 2. 하드코딩 noise 패턴
    for pattern in NOISE_FROM_PATTERNS:
        if pattern in frm_lower:
            return ("noise", f"발신자 패턴 '{pattern}'")

    return (None, "")


def classify_emails_llm(emails: list[dict]) -> dict[str, tuple[str, str]]:
    """LLM (claude opus 4.7) 으로 batch 분류. 반환: {msg_id: (category, reason)}.
    실패 시 빈 dict (호출자가 pending fallback).
    confidence=low 응답은 안전하게 pending 으로 강등."""
    if not emails:
        return {}

    items_text = []
    for i, m in enumerate(emails, 1):
        items_text.append(
            f"[{i}] id={m.get('id')}\n"
            f"  from: {m.get('from','')}\n"
            f"  subject: {m.get('subject','')}\n"
            f"  date: {m.get('date','')}"
        )

    # SSOT: vault gmail-capture.md §1·§2 (사용자 컨텍스트·few-shot 포함).
    # 추출 실패 시에만 인라인 fallback.
    vault_guide = _vault_classification_guide()
    if vault_guide:
        guide_block = (
            "분류 기준 — 아래는 2nd-brain-vault `gmail-capture.md` §1·§2 의 권위 정의다 "
            "(사용자 컨텍스트 + few-shot 예시 포함). 이 정의를 따르라.\n\n"
            f"{vault_guide}\n"
        )
    else:
        guide_block = (
            "분류 기준 (라벨은 '브레인화 작업 진행 상태' 표시, 콘텐츠 카테고리 아님 — "
            "vault gmail-capture.md 추출 실패로 인라인 fallback):\n"
            "- proceed: 영수증/증명서/결제확인/계약서/학회·병원 공문 등 거래·증빙 — "
            "audit trail 가치 있어 브레인화로 진행 (라벨 '브레인화/진행' + archive)\n"
            "- pending: 외부 사이트 다운로드·후속 행동 필요 또는 위 두 분류에 자신없음 — "
            "라벨 '브레인화/보류' + inbox 유지\n"
            "- noise: 광고/뉴스레터/자동 알림/명백한 중복 — 브레인화 대상 아님 "
            "(라벨 '브레인화/불필요' + archive)\n"
        )

    today = now_kst()
    today_str = f"{today.date().isoformat()} ({DOW_KR[today.date().weekday()]})"

    prompt = (
        f"다음 {len(emails)}개 Gmail 메일을 분류하라. **반드시 JSON 배열만 응답하라** (다른 텍스트 금지).\n\n"
        f"오늘 날짜 (KST): {today_str}\n"
        f"  → '이미 지난 행사' / '예정된 행사' 판단 시 이 기준으로 비교할 것.\n\n"
        f"{guide_block}\n"
        "출력 카테고리 (3 중 하나) + 자신감 (3 중 하나):\n"
        "- proceed → 라벨 '브레인화/진행' + archive\n"
        "- pending → 라벨 '브레인화/보류' + inbox 유지\n"
        "- noise   → 라벨 '브레인화/불필요' + archive (명백한 중복은 trash)\n"
        "- confidence: high | med | low\n"
        "  high = 발신자/제목/예시 매칭이 명확.\n"
        "  med  = 합리적 추정이나 미세한 모호함 존재.\n"
        "  low  = 자신없음. (low 응답은 시스템이 자동으로 pending 으로 강등하니, "
        "정말 모를 때만 사용하라.)\n\n"
        "응답 형식 (JSON 배열만):\n"
        '[{"id":"<msg_id>","category":"proceed|pending|noise","confidence":"high|med|low","reason":"한 줄 이유"}]\n\n'
        "메일 목록:\n" + "\n\n".join(items_text)
    )

    # prompt 는 stdin 으로 전달. --disallowedTools 가 variadic 옵션이라
    # 그 뒤에 positional prompt 를 두면 옵션이 prompt 를 흡수해 "Input must be provided"
    # 에러가 난다 (commander.js variadic semantics).
    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-opus-4-7",
        "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("[gws-assistant] LLM 분류 timeout", file=sys.stderr)
        return {}
    if r.returncode != 0:
        print(f"[gws-assistant] LLM 분류 실패 exit={r.returncode}: {(r.stderr or '')[:500]}", file=sys.stderr)
        return {}

    out = (r.stdout or "").strip()
    # markdown 코드블록 제거
    if "```" in out:
        # ```json ... ``` 또는 ``` ... ```
        try:
            inner = out.split("```", 2)[1]
            if inner.startswith("json"):
                inner = inner[4:]
            out = inner.strip()
            if out.endswith("```"):
                out = out[:-3].strip()
        except IndexError:
            pass
    # JSON array 추출
    try:
        results = json.loads(out)
    except json.JSONDecodeError:
        try:
            start = out.index("[")
            end = out.rindex("]") + 1
            results = json.loads(out[start:end])
        except (ValueError, json.JSONDecodeError):
            print(f"[gws-assistant] LLM 응답 JSON 파싱 실패: {out[:300]}", file=sys.stderr)
            return {}

    valid_cats = VALID_CATEGORIES
    by_id = {}
    if isinstance(results, list):
        for r_item in results:
            if not isinstance(r_item, dict):
                continue
            mid = r_item.get("id")
            cat = r_item.get("category", "pending")
            reason = (r_item.get("reason") or "").strip()
            confidence = (r_item.get("confidence") or "med").lower()
            if not mid or cat not in valid_cats:
                continue
            # 안전장치: low confidence 는 pending 으로 강등.
            # 이미 pending 인 경우는 reason 만 보강.
            if confidence == "low" and cat != "pending":
                reason = f"(자신없음 → 보류 강등; LLM 1차 판단={cat}) {reason}"
                cat = "pending"
            by_id[mid] = (cat, reason or "(이유 없음)")
    return by_id


def make_action(category: str) -> dict:
    if category == "noise":
        return {"labels_add": [LABEL_NOISE], "archive": True}
    if category == "proceed":
        return {"labels_add": [LABEL_PROCEED], "archive": True}
    if category == "pending":
        return {"labels_add": [LABEL_PENDING]}
    return {}


def _make_item(m: dict, category: str, reason: str) -> dict:
    item = {
        "msg_id": m.get("id"),
        "from": m.get("from", ""),
        "subject": m.get("subject", "(제목 없음)"),
        "date": m.get("date", ""),
        "category": category,
        "reason": reason,
        "action": make_action(category),
    }
    if category == "proceed":
        item["confirm_status"] = "pending_review"
        item["note_path"] = None
        item["proposed_para_path"] = None
        item["proposed_links"] = []
        item["body_summary"] = ""
        item["attachments"] = []
    return item


def build_plan_items(emails: list, existing_plan: dict | None) -> list[dict]:
    """휴리스틱 noise 우선 분류 → 나머지는 LLM batch 호출. 캐시: existing_plan
    의 같은 msg_id 는 재분류하지 않음 (분류 안정성·비용 절약)."""
    cached = {}
    if existing_plan:
        for it in existing_plan.get("items", []):
            mid = it.get("msg_id")
            if mid:
                cached[mid] = it

    items: list[dict] = []
    needs_llm: list[dict] = []
    for m in emails:
        mid = m.get("id")
        if mid in cached:
            it = dict(cached[mid])
            # 표층 필드만 갱신 (Gmail 측에서 변경됐을 수도)
            it["from"] = m.get("from", it.get("from", ""))
            it["subject"] = m.get("subject", it.get("subject", "(제목 없음)"))
            it["date"] = m.get("date", it.get("date", ""))
            items.append(it)
            continue
        cat, reason = classify_email_heuristic(m)
        if cat is not None:
            items.append(_make_item(m, cat, reason))
        else:
            needs_llm.append(m)

    if needs_llm:
        llm_results = classify_emails_llm(needs_llm)
        for m in needs_llm:
            res = llm_results.get(m.get("id"))
            if res:
                cat, reason = res
            else:
                cat, reason = ("pending", "LLM 분류 실패 — inbox 유지")
            items.append(_make_item(m, cat, reason))

    return items


# ============================================================================
# Plan merge
# ============================================================================

def sort_plan_items(items: list[dict]) -> list[dict]:
    """CATEGORY_ORDER 우선순위 + 같은 분류 안에선 date 역순 (최신이 위, Gmail 받은편지함 순서와 일치).
    그룹별로 명시 재정렬해 캐시 머지·heuristic vs LLM 분류 순서 차이가 결과에 영향 안 미치도록 보장."""
    def cat_idx(it):
        cat = it.get("category", "pending")
        try:
            return CATEGORY_ORDER.index(cat)
        except ValueError:
            return len(CATEGORY_ORDER)
    by_cat: dict[int, list[dict]] = {}
    for it in items:
        by_cat.setdefault(cat_idx(it), []).append(it)
    out: list[dict] = []
    for idx in sorted(by_cat.keys()):
        out.extend(sorted(by_cat[idx], key=lambda it: it.get("date", ""), reverse=True))
    return out


def merge_plan(state: dict, emails: list[dict], now: dt.datetime) -> dict:
    """현재 받은편지함 미분류 emails 를 분류(캐시 활용) 후 pending plan 에 머지.
    - 신규 메일만 LLM 호출. 기존 plan 의 msg_id 는 분류 그대로 재사용.
    - 외부에서 처리된 (검색 결과에서 사라진) msg_id 는 plan 에서 제거.
    - plan 의 created_at 은 첫 생성 시점 유지.
    - items 는 카테고리 우선순위로 정렬해 표시 안정성 확보."""
    existing = state.get("pending_plan") or {}
    fresh_items = build_plan_items(emails, existing)
    fresh_items = sort_plan_items(fresh_items)
    plan = {
        "plan_id": existing.get("plan_id") or now.isoformat(),
        "created_at": existing.get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
        "last_announced_msg_ids": existing.get("last_announced_msg_ids", []),
        "items": fresh_items,
    }
    return plan


# ============================================================================
# Formatting
# ============================================================================

def fmt_date_kr(d: dt.date) -> str:
    return f"{d.isoformat()} ({DOW_KR[d.weekday()]})"


def fmt_email_line(num: int, m: dict) -> str:
    subj = m.get("subject") or "(제목 없음)"
    frm = m.get("from") or ""
    date = m.get("date") or ""
    mid = m.get("msg_id") or m.get("id") or ""
    id_tag = f"[{mid}] " if mid else ""
    return f"{num}. {id_tag}{date}  {frm}\n   제목: {subj}"


def format_plan_message(now: dt.datetime, plan: dict, new_count: int, total_count: int) -> str:
    items = plan.get("items", [])
    by_cat: dict[str, list] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)

    n_noise = len(by_cat.get("noise", []))
    n_proceed = len(by_cat.get("proceed", []))
    n_pend = len(by_cat.get("pending", []))

    L = []
    L.append(f"[Gmail 브리핑] {fmt_date_kr(now.date())} {now.strftime('%H:%M')}")
    L.append("")
    if new_count == total_count:
        L.append(f"받은편지함 미분류 메일 {total_count}건이 있습니다. 브레인화 진행/불필요/보류로 분류한 것을 검토 바랍니다.")
    else:
        L.append(f"받은편지함 미분류 메일 {total_count}건이 있습니다 (이번에 신규 {new_count}건 추가). 브레인화 진행/불필요/보류로 분류한 것을 검토 바랍니다.")
    L.append("")

    def emit_section(header_text: str, items_list: list[dict]):
        if not items_list:
            return
        L.append(header_text)
        L.append("")
        for i, it in enumerate(items_list, 1):
            L.append(fmt_email_line(i, it))
            if it.get("reason"):
                L.append(f"   분류 사유: {it['reason']}")
            L.append("")

    proceed_header = f"▸ 진행 {n_proceed}건"
    pending_header = f"▸ 보류 {n_pend}건"
    noise_header = f"▸ 불필요 {n_noise}건"

    emit_section(proceed_header, by_cat.get("proceed", []))
    emit_section(pending_header, by_cat.get("pending", []))
    emit_section(noise_header, by_cat.get("noise", []))

    L.append("[명령]")
    L.append("  /g 1 (=승인)")
    L.append("  /g 2 (=재분류)")
    L.append("  /g 3 (=취소)")
    L.append("")
    L.append(f"[plan id] {plan.get('plan_id', '')}")
    L.append(f"[처리 시각] {now.isoformat()}")
    return "\n".join(L)


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


def propose_proceed(item: dict) -> tuple[bool, str]:
    """Brainify Phase 1 — 본문 fetch + 동반 노트 작성 + atomic write. **라벨/archive 안 함.**
    item을 in-place로 채움: note_path / proposed_para_path / proposed_links / body_summary /
    attachments / confirm_status='pending_review'.
    Returns (ok, error_or_empty)."""
    thread_id = item.get("msg_id")
    if not thread_id:
        return (False, "thread_id 없음")

    attach_dir = _attachment_dir(thread_id)
    payload = fetch_thread_full(thread_id, out_dir=attach_dir)
    if payload is None:
        return (False, "thread fetch 실패")

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


def finalize_proceed(item: dict, label: str = LABEL_DONE) -> tuple[bool, str]:
    """Brainify Phase 2 — 사용자 액션 후:
    1) PARA 폴더로 노트·첨부 이동 (proposed_para_path 가 있으면)
    2) 라벨 부착 + archive
       - 기본 LABEL_DONE (확정/할일/경로수정 — 즉시 종결)
       - LABEL_PROCEED (답장/답장할일 — 발송 대기, awaiting_reply 폴링이 자동 promote)
    노트 자체는 propose 단계에서 이미 작성됨.
    item['final_label'] 에 실제 부착된 라벨 기록 (출력용)."""
    thread_id = item.get("msg_id")
    if not thread_id:
        return (False, "thread_id 없음")

    para = _normalize_para_coord(item.get("proposed_para_path") or "")
    if para:
        ok, err = _relocate_to_para(item, para)
        if not ok:
            return (False, f"PARA 이동 실패: {err}")

    ok, err = gog_call(
        "gmail", "labels", "modify", thread_id,
        "--add", label, "--remove", "INBOX",
    )
    if not ok:
        return (False, f"라벨/archive 실패: {err}")
    item["confirm_status"] = "confirmed"
    item["final_label"] = label
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


def update_proceed_note(
    item: dict,
    new_para: str | None,
    new_links: list[str] | None,
) -> tuple[bool, str]:
    """edit 명령으로 받은 새 PARA 경로/링크를 노트 frontmatter에 반영 (atomic rewrite)."""
    note_rel = item.get("note_path")
    if not note_rel:
        return (False, "note_path 없음 — propose 안 됨")
    note_path = VAULT_ROOT / note_rel
    if not note_path.exists():
        return (False, f"노트 파일 없음: {note_rel}")
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError as e:
        return (False, f"노트 읽기 실패: {e}")

    if new_para is not None:
        text = _replace_fm_field(text, "proposed_para_path", new_para)
        item["proposed_para_path"] = new_para
    if new_links is not None:
        text = _replace_fm_field_list(text, "proposed_links", new_links)
        item["proposed_links"] = new_links

    try:
        fd, tmp = tempfile.mkstemp(prefix=".gws-note.", dir=str(note_path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, note_path)
    except OSError as e:
        return (False, f"노트 쓰기 실패: {e}")
    return (True, "")


# ============================================================================
# Approval execution
# ============================================================================

def apply_label_action(msg_id: str, action: dict) -> tuple[bool, str]:
    """Pure label/archive (no body fetch, no note). Used for pending/noise."""
    add_labels = action.get("labels_add", [])
    archive = action.get("archive", False)
    if not add_labels and not archive:
        return (True, "")
    args = ["gmail", "labels", "modify", msg_id]
    for label in add_labels:
        args += ["--add", label]
    if archive:
        args += ["--remove", "INBOX"]
    ok, err = gog_call(*args)
    if not ok:
        return (False, f"label/archive: {err}")
    return (True, "")


# ============================================================================
# Queue helpers — proceed 항목별 1:1 confirm 흐름
# ============================================================================

def _proceed_items(plan: dict) -> list[dict]:
    return [it for it in plan.get("items", []) if it.get("category") == "proceed"]


def _next_pending_review(plan: dict) -> dict | None:
    for it in _proceed_items(plan):
        if it.get("confirm_status") == "pending_review":
            return it
    return None


def _count_pending_review(plan: dict) -> int:
    """proceed 큐에서 confirm_status="pending_review" 인 항목 수 — confirm/edit/skip/dismiss
    이후 잔여 큐 가시성 표시용."""
    return sum(
        1 for it in _proceed_items(plan)
        if it.get("confirm_status") == "pending_review"
    )


def _proceed_review_index(plan: dict, item: dict) -> tuple[int, int]:
    """Return (1-based index, total) of `item` within proceed items."""
    proceed = _proceed_items(plan)
    total = len(proceed)
    for i, it in enumerate(proceed, 1):
        if it.get("msg_id") == item.get("msg_id"):
            return (i, total)
    return (0, total)


def _find_item_by_thread(plan: dict, thread_id: str) -> dict | None:
    for it in plan.get("items", []):
        if it.get("msg_id") == thread_id:
            return it
    return None


def _require_pending_review(plan: dict | None, thread_id: str) -> tuple[dict | None, str]:
    """후속조치 명령 전제 검사: 대상 thread 가 큐의 현재 review 항목이어야 한다.
    Returns (item, "") on success, (None, error_message) on rejection."""
    if not plan:
        return (None, "pending plan 이 없습니다.")
    item = _find_item_by_thread(plan, thread_id)
    if not item:
        return (None, f"thread_id '{thread_id}' 항목을 찾을 수 없습니다.")
    if item.get("category") != "proceed":
        return (None, f"proceed 항목이 아닙니다 (category={item.get('category')}).")
    if item.get("confirm_status") != "pending_review":
        return (None, f"이미 처리된 항목입니다 (status={item.get('confirm_status')}).")
    return (item, "")


def _resolve_pending_review_thread(
    plan: dict | None, thread_id: str | None,
) -> tuple[dict | None, str]:
    """thread_id 가 명시되면 그것으로, 생략 시 review 대기 항목이 정확히 1건이면 자동 보강.
    cmd_nl 의 단일 pending_review 자동 보강 패턴과 동일한 규칙을 confirm/edit/skip/dismiss
    에서 공유하기 위한 헬퍼. Returns (item, "") on success, (None, err) on rejection."""
    if not plan:
        return (None, "pending plan 이 없습니다.")
    if thread_id:
        item = _find_item_by_thread(plan, thread_id)
        if not item:
            return (None, f"thread_id '{thread_id}' 항목을 찾을 수 없습니다.")
        if (item.get("category") != "proceed"
                or item.get("confirm_status") != "pending_review"):
            return (None, f"thread_id '{thread_id}' 가 review 대기 상태가 아닙니다.")
        return (item, "")
    candidates = [
        it for it in _proceed_items(plan)
        if it.get("confirm_status") == "pending_review"
    ]
    if len(candidates) == 0:
        return (None, "현재 review 대기 항목이 없습니다.")
    if len(candidates) > 1:
        tids = ", ".join(it.get("msg_id", "") for it in candidates)
        return (None, f"review 대기 항목이 여러 개입니다 ({tids}) — thread_id 를 명시하세요.")
    return (candidates[0], "")


def _parse_kv_args(argv: list[str], keys: tuple[str, ...]) -> dict[str, str]:
    """`when=...` `duration=...` 같은 key=value 인자를 dict 로 파싱.
    값에 공백이 있을 수 있으므로 argv 결합 후 다음 known key 직전까지 캡처."""
    rest = " ".join(argv)
    out: dict[str, str] = {}
    key_alt = "|".join(re.escape(k) for k in keys)
    for k in keys:
        pat = re.compile(
            rf"(?:^|\s){re.escape(k)}=(.+?)(?=\s+(?:{key_alt})=|$)",
            re.DOTALL,
        )
        m = pat.search(rest)
        if m:
            out[k] = m.group(1).strip().strip('"').strip("'")
    return out


def _parse_edit_args(argv: list[str]) -> tuple[str, str | None, list[str] | None]:
    """edit [thread_id] [folder=…] [links=<[[A]],[[B]]>] 파싱.
    thread_id 생략 시 첫 토큰이 'key=value' 형태이면 tid="" 로 반환 (호출측에서 자동 보강)."""
    if not argv:
        return ("", None, None)
    if "=" in argv[0]:
        tid = ""
        rest_argv = argv
    else:
        tid = argv[0]
        rest_argv = argv[1:]
    folder: str | None = None
    links: list[str] | None = None
    # links= 값에 쉼표가 있을 수 있으니, argv 결합 후 key= 분리.
    rest = " ".join(rest_argv)
    # split by token boundaries " folder=" or " links=" while preserving order.
    # 간단화: positional 파라미터로 'folder=' / 'links=' 만 찾는다.
    m_folder = re.search(r"(?:^|\s)folder=(.+?)(?=\s+(?:folder=|links=)|$)", rest)
    if m_folder:
        folder = m_folder.group(1).strip().strip('"').strip("'")
    m_links = re.search(r"(?:^|\s)links=(.+?)(?=\s+(?:folder=|links=)|$)", rest)
    if m_links:
        raw = m_links.group(1).strip()
        parts = [p.strip().strip('"').strip("'") for p in raw.split(",") if p.strip()]
        links = parts
    return (tid, folder, links)


# ============================================================================
# Formatting — proposal / label-only / completion messages
# ============================================================================

def _short_subject(s: str, n: int = 50) -> str:
    s = s or "(제목 없음)"
    return s if len(s) <= n else s[: n - 1] + "…"


def format_proposal_message(now: dt.datetime, item: dict, idx: int, total: int) -> str:
    L: list[str] = []
    L.append(f"[브레인화 {idx}/{total}] {item.get('subject', '(제목 없음)')}")
    L.append(f"발신: {item.get('from','')}")
    L.append(f"일자: {item.get('date','')}")
    L.append("")
    note_path = item.get("note_path") or "(작성 실패)"
    L.append(f"동반 노트 (staging): {note_path}")
    para_raw = item.get("proposed_para_path") or ""
    para = _normalize_para_coord(para_raw)
    attachments = item.get("attachments") or []
    if para:
        L.append(f"제안 PARA 좌표: {para}/")
        L.append(f"  → 확정 시 노트:  knowledge/{para}/")
        if attachments:
            L.append(f"  → 확정 시 첨부:  sources/{para}/  ({len(attachments)}건)")
    else:
        L.append(f"제안 PARA 좌표: (미제안 — staging sources/00_inbox/ 유지)")
    links = item.get("proposed_links") or []
    L.append(f"연결 후보: {', '.join(links) if links else '(없음)'}")
    if attachments:
        head = ", ".join(attachments[:3])
        suffix = f" 외 {len(attachments) - 3}건" if len(attachments) > 3 else ""
        L.append(f"현재 첨부 위치: {len(attachments)}건 — {head}{suffix}")
    summary = item.get("body_summary") or ""
    if summary:
        L.append("")
        L.append("## 요약 미리보기")
        L.append(summary)
    followups = item.get("followups") or []
    if followups:
        L.append("")
        L.append("[기록된 후속조치]")
        for fl in followups:
            kind = fl.get("kind", "?")
            if kind == "draft-reply":
                L.append(f"  · 회신 초안 (Drafts) — to {fl.get('to','')} "
                         f"[{fl.get('at','')}]")
            elif kind == "schedule":
                L.append(f"  · 일정 등록 — {fl.get('summary','')} "
                         f"@ {fl.get('start','')}")
            elif kind == "replied":
                note = f" — {fl.get('summary')}" if fl.get("summary") else ""
                L.append(f"  · 회신 완료 기록 [{fl.get('at','')}]{note}")
            elif kind == "todo":
                L.append(f"  · 후속 액션: {fl.get('text','')}")
            else:
                L.append(f"  · {kind} [{fl.get('at','')}]")
    L.append("")
    L.append("[명령]  (하나만 선택 — 1단계 종결)")
    L.append(f"  /g 1                              → 저장      (PARA 이동 + '{LABEL_DONE}' + archive)")
    L.append(f"  /g 2 [톤·요지]                    → 답장      (Drafts + '{LABEL_PROCEED}' + awaiting_reply)")
    L.append("  /g 3 [YYYY-MM-DD] [지시]          → 답장할일  (답장 + Google Tasks 등록)")
    L.append("  /g 4 [YYYY-MM-DD] [메모]          → 할일      (Google Tasks 등록 + 즉시 종결)")
    L.append("  /g 5 [YYYY-MM-DD HH:MM]           → 일정      (Google Calendar 등록 + 즉시 종결)")
    L.append("  /g 6 <경로>                       → 경로수정  (PARA 폴더 변경 + 즉시 종결)")
    L.append(f"  /g 보류                           → 노트 삭제 + '{LABEL_PENDING}' (inbox 유지)")
    L.append(f"  /g 불필요                         → 노트 삭제 + '{LABEL_NOISE}' + archive")
    return "\n".join(L)


def format_label_only_result(now: dt.datetime, summary: dict) -> str:
    L: list[str] = []
    L.append(f"[브레인화 라벨 처리] {fmt_date_kr(now.date())} {now.strftime('%H:%M')}")
    L.append("")
    L.append(
        f"  · 보류 {summary.get('pending_ok', 0)}건, "
        f"불필요 {summary.get('noise_ok', 0)}건 처리됨."
    )
    fails = summary.get("pending_fail", 0) + summary.get("noise_fail", 0)
    if fails:
        L.append(f"  · 실패: {fails}건")
        for e in (summary.get("errors") or [])[:5]:
            L.append(f"    - {e}")
    return "\n".join(L)


def format_proceed_complete(now: dt.datetime, plan: dict) -> str:
    proceed = _proceed_items(plan)
    confirmed = sum(1 for it in proceed if it.get("confirm_status") == "confirmed")
    skipped = sum(1 for it in proceed if it.get("confirm_status") == "skipped")
    failed = sum(
        1 for it in proceed
        if it.get("confirm_status") not in ("confirmed", "skipped")
    )
    L: list[str] = []
    L.append(f"[브레인화 완료] {fmt_date_kr(now.date())} {now.strftime('%H:%M')}")
    L.append("")
    L.append(f"  · 확정: {confirmed}건  · 건너뛰기: {skipped}건"
             + (f"  · 미처리: {failed}건" if failed else ""))
    L.append("")
    L.append("plan 종료. 다음 폴링에서 새 plan 생성.")
    return "\n".join(L)


# ============================================================================
# Snooze
# ============================================================================

def is_snoozed(state: dict, now: dt.datetime) -> bool:
    su = state.get("snooze_until")
    if not su:
        return False
    try:
        until = dt.datetime.fromisoformat(su)
    except (ValueError, TypeError):
        return False
    return now < until


# ============================================================================
# Subcommand handlers
# ============================================================================

def _safety_bulk_label(plan: dict) -> str:
    """confirm/edit/skip/dismiss 진입 시 미처리 pending/noise 가 남아 있으면 라벨 일괄 처리.
    approve 를 건너뛰고 reclassify 후 곧장 proceed 처리로 진입한 경우의 안전망.
    함수는 idempotent — 이미 confirmed 인 항목은 _bulk_label_pending_noise 에서 스킵됨.
    Returns 보고용 헤더 라인 (작업 없으면 빈 문자열)."""
    summary = _bulk_label_pending_noise(plan)
    ok_total = summary.get("pending_ok", 0) + summary.get("noise_ok", 0)
    if ok_total == 0:
        return ""
    return (
        f"[approve 누락 보강] 보류 {summary['pending_ok']}건, "
        f"불필요 {summary['noise_ok']}건 라벨/archive 처리됨.\n\n"
    )


def _bulk_label_pending_noise(plan: dict) -> dict:
    """pending/noise 항목들을 라벨/archive 일괄 처리. proceed 는 건드리지 않음."""
    summary = {
        "pending_ok": 0, "pending_fail": 0,
        "noise_ok": 0, "noise_fail": 0,
        "errors": [],
    }
    for it in plan.get("items", []):
        cat = it.get("category", "")
        if cat not in ("pending", "noise"):
            continue
        if it.get("confirm_status") == "confirmed":
            continue
        msg_id = it.get("msg_id", "")
        if not msg_id:
            summary[f"{cat}_fail"] += 1
            summary["errors"].append(f"[{cat}] msg_id 없음")
            continue
        ok, err = apply_label_action(msg_id, it.get("action") or {})
        if ok:
            summary[f"{cat}_ok"] += 1
            it["confirm_status"] = "confirmed"
        else:
            summary[f"{cat}_fail"] += 1
            summary["errors"].append(f"[{cat}] {msg_id}: {err}")
    return summary


def _propose_next_or_complete(
    state: dict, plan: dict, now: dt.datetime, header: str
) -> str:
    """다음 pending_review 항목을 propose 하고 메시지 반환. 없으면 완료 메시지.
    state["pending_plan"] 도 함께 갱신."""
    next_item = _next_pending_review(plan)
    if next_item is None:
        msg = (header + format_proceed_complete(now, plan)) if header else format_proceed_complete(now, plan)
        state["pending_plan"] = None
        return msg
    ok, err = propose_proceed(next_item)
    idx, total = _proceed_review_index(plan, next_item)
    if not ok:
        next_item["confirm_status"] = "pending_review"
        body = f"[다음 항목 propose 실패] {err}\n해당 항목은 큐에 남아있습니다. 다시 시도하려면 cron 폴링 또는 cancel/재발화."
    else:
        body = format_proposal_message(now, next_item, idx, total)
    state["pending_plan"] = plan
    return (header + body) if header else body


def cmd_approve(state: dict, now: dt.datetime) -> int:
    """큐 모델: pending/noise 일괄 처리 + 첫 proceed 1건만 propose 후 출력."""
    plan = state.get("pending_plan")
    if not plan or not plan.get("items"):
        print("[브레인화] pending plan 이 없습니다. 먼저 폴링이 발화해야 합니다.")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    label_summary = _bulk_label_pending_noise(plan)

    # proceed 항목 confirm_status 초기화 (legacy 캐시 호환)
    for it in plan.get("items", []):
        if it.get("category") == "proceed" and not it.get("confirm_status"):
            it["confirm_status"] = "pending_review"

    proceed_total = len(_proceed_items(plan))
    if proceed_total == 0:
        msg = format_label_only_result(now, label_summary)
        msg += "\n\nplan 에 brainify 진행 항목이 없어 종료합니다. 다음 폴링에서 새 plan 생성."
        state["pending_plan"] = None
        print(msg)
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    header_lines: list[str] = []
    if label_summary["pending_ok"] + label_summary["noise_ok"] > 0:
        header_lines.append(
            f"[라벨 일괄 처리] 보류 {label_summary['pending_ok']}건, "
            f"불필요 {label_summary['noise_ok']}건 처리됨."
        )
        header_lines.append("")
    header_lines.append(
        f"진행 {proceed_total}건은 1건씩 검토합니다. 첫 항목을 보고드립니다 ↓"
    )
    header_lines.append("")
    header = "\n".join(header_lines) + "\n"

    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def cmd_confirm(state: dict, now: dt.datetime, thread_id: str | None = None) -> int:
    """thread_id 명시 시 해당 항목, 생략 시 단일 pending_review 항목을 PARA 로 확정 이동
    + 라벨/archive. 진입 시 미처리 pending/noise 가 있으면 함께 라벨 일괄 처리 (safety net)."""
    plan = state.get("pending_plan")
    item, err = _resolve_pending_review_thread(plan, thread_id)
    if not item:
        print(f"[브레인화] {err}")
        return 0

    safety = _safety_bulk_label(plan)

    ok, err = finalize_proceed(item)
    if not ok:
        print(f"[브레인화 확정 실패] {err}\n해당 항목은 큐에 남아있습니다.")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    para = item.get("proposed_para_path") or "sources/00_inbox/"
    residual = _count_pending_review(plan)
    final_label = item.get("final_label") or LABEL_PROCEED
    header = (
        safety
        + f"[브레인화 확정] {_short_subject(item.get('subject',''), 40)} "
        + f"→ 노트 위치 {para}, 라벨 '{final_label}' + archive\n"
        + f"  · 잔여 review 대기 {residual}건\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def cmd_edit(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """thread_id 명시 시 해당 항목, 생략 시 단일 pending_review 항목의 노트를 수정 후 확정.
    진입 시 미처리 pending/noise 가 있으면 함께 라벨 일괄 처리 (safety net)."""
    thread_id, folder, links = _parse_edit_args(argv)
    plan = state.get("pending_plan")
    item, err = _resolve_pending_review_thread(plan, thread_id or None)
    if not item:
        print(f"[브레인화] {err}")
        return 0
    if folder is None and links is None:
        print("[브레인화] edit 에 folder= 또는 links= 중 하나는 필요합니다.")
        return 1

    safety = _safety_bulk_label(plan)

    ok, err = update_proceed_note(item, folder, links)
    if not ok:
        print(f"[브레인화 노트 수정 실패] {err}\n해당 항목은 큐에 남아있습니다.")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0
    ok2, err2 = finalize_proceed(item)
    if not ok2:
        print(f"[브레인화 확정 실패] {err2}\n노트는 수정됐으나 라벨/archive 가 실패했습니다.")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    para = item.get("proposed_para_path") or "sources/00_inbox/"
    residual = _count_pending_review(plan)
    final_label = item.get("final_label") or LABEL_PROCEED
    header = (
        safety
        + f"[브레인화 수정 후 확정] {_short_subject(item.get('subject',''), 40)} "
        + f"→ 노트 위치 {para}, 라벨 '{final_label}' + archive\n"
        + f"  · 잔여 review 대기 {residual}건\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def cmd_dismiss(state: dict, now: dt.datetime, thread_id: str | None = None) -> int:
    """proposed 단계 항목을 '불필요' 로 폐기 (skip 의 noise 버전).
    노트 삭제 + 라벨 '브레인화/불필요' + archive + 다음 큐 propose.
    thread_id 생략 시 단일 pending_review 항목 자동 보강.
    진입 시 미처리 pending/noise 가 있으면 함께 라벨 일괄 처리 (safety net)."""
    plan = state.get("pending_plan")
    item, err = _resolve_pending_review_thread(plan, thread_id)
    if not item:
        print(f"[브레인화] {err}")
        return 0
    thread_id = item.get("msg_id", "")

    safety = _safety_bulk_label(plan)

    note_rel = item.get("note_path")
    if note_rel:
        note_full = VAULT_ROOT / note_rel
        try:
            note_full.unlink(missing_ok=True)
        except OSError:
            pass

    ok, err = gog_call(
        "gmail", "labels", "modify", thread_id,
        "--add", LABEL_NOISE, "--remove", "INBOX",
    )
    if not ok:
        print(f"[브레인화 폐기 실패] {err}\n해당 항목은 큐에 남아있습니다.")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0
    item["confirm_status"] = "dismissed"

    residual = _count_pending_review(plan)
    header = (
        safety
        + f"[브레인화 폐기] {_short_subject(item.get('subject',''), 40)} "
        + f"→ 라벨 '{LABEL_NOISE}' + archive. 노트 파일 정리됨.\n"
        + f"  · 잔여 review 대기 {residual}건\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def cmd_skip(state: dict, now: dt.datetime, thread_id: str | None = None) -> int:
    """thread_id 명시 시 해당 항목, 생략 시 단일 pending_review 항목을 보류 처리.
    진입 시 미처리 pending/noise 가 있으면 함께 라벨 일괄 처리 (safety net)."""
    plan = state.get("pending_plan")
    item, err = _resolve_pending_review_thread(plan, thread_id)
    if not item:
        print(f"[브레인화] {err}")
        return 0
    thread_id = item.get("msg_id", "")

    safety = _safety_bulk_label(plan)

    note_rel = item.get("note_path")
    if note_rel:
        note_full = VAULT_ROOT / note_rel
        try:
            note_full.unlink(missing_ok=True)
        except OSError:
            pass

    ok, err = gog_call("gmail", "labels", "modify", thread_id, "--add", LABEL_PENDING)
    if not ok:
        print(f"[브레인화 skip 실패] {err}\n해당 항목은 큐에 남아있습니다.")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0
    item["confirm_status"] = "skipped"

    residual = _count_pending_review(plan)
    header = (
        safety
        + f"[브레인화 건너뛰기] {_short_subject(item.get('subject',''), 40)} "
        + f"→ 라벨 '{LABEL_PENDING}' (inbox 유지). 노트 파일 정리됨.\n"
        + f"  · 잔여 review 대기 {residual}건\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


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


def cmd_draft_reply(state: dict, now: dt.datetime, argv: list[str],
                    create_gtask_too: bool = False,
                    user_due: str | None = None) -> int:
    """현재 review 항목 → vault + Gmail 컨텍스트 검색 → Opus 4.7 회신 초안 → Gmail Drafts 등록.
    노트 PARA 이동 + LABEL_PROCEED + archive + awaiting_reply 큐 등록.
    발송 자동 감지 폴링이 LABEL_PROCEED → LABEL_DONE 으로 promote.

    create_gtask_too=True 면 Google Tasks 도 함께 등록 (`/g 답장할일` 합성 호출).
    user_due: 답장할일 합성 시 명시적 마감일 ('YYYY-MM-DD').

    Usage: draft-reply [thread_id] [지시…]"""
    plan = state.get("pending_plan")

    thread_id: str | None = None
    instruction = ""
    if argv:
        first = argv[0]
        if re.fullmatch(r"[0-9a-fA-F]{8,}", first):
            thread_id = first
            instruction = " ".join(argv[1:]).strip()
        else:
            instruction = " ".join(argv).strip()

    item, err = _resolve_pending_review_thread(plan, thread_id)
    if not item:
        print(f"[브레인화] {err}")
        return 0
    thread_id = item.get("msg_id")

    payload = fetch_thread_full(thread_id, out_dir=None)
    if payload is None:
        print("[브레인화 답장 실패] thread fetch 실패. 큐 상태 유지.")
        return 0
    msg = _pick_target_message(payload, thread_id)
    if not msg:
        print("[브레인화 답장 실패] 메시지 추출 실패. 큐 상태 유지.")
        return 0
    headers = _headers_to_dict(msg.get("payload", {}).get("headers", []))
    frm = headers.get("from", "")
    subject = headers.get("subject", "(제목 없음)")
    reply_to = headers.get("reply-to", "") or frm
    body_text = _extract_plain_text(msg.get("payload") or {}) or msg.get("snippet", "")

    vault_ctx = _search_vault_context(item)
    prior_threads = _search_prior_threads(item)

    prompt_parts = [
        "다음 Gmail 메일에 대한 한국어 회신 초안을 작성하라.",
        "Dr. Ben(benkorea.ai@gmail.com)이 보낼 회신이며, 정중한 존댓말을 사용한다.",
        "응답은 회신 본문만 — 인사·서명 포함 가능, 코드블록·메타 설명·해설은 금지.",
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
        prompt_parts += ["[vault 관련 노트 발췌 — 회신 톤·맥락 참고용]", vault_ctx, ""]
    prompt_parts += [
        "[사용자 지시]",
        instruction or "(특별한 지시 없음 — 의례적 회신 초안 작성)",
    ]
    prompt = "\n".join(prompt_parts)

    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-opus-4-7",
        "--disallowedTools",
        "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print("[브레인화 답장 실패] LLM timeout. 큐 상태 유지.")
        return 0
    if r.returncode != 0:
        print(f"[브레인화 답장 실패] LLM exit={r.returncode}.")
        return 0
    body = (r.stdout or "").strip()
    if body.startswith("```"):
        nl = body.find("\n")
        body = body[nl + 1:] if nl >= 0 else body[3:]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        body = body.strip()
    if not body:
        print("[브레인화 답장 실패] LLM 빈 응답. 큐 상태 유지.")
        return 0

    re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    fd_b, body_file = tempfile.mkstemp(prefix=".gws-reply.", suffix=".txt")
    try:
        with os.fdopen(fd_b, "w", encoding="utf-8") as f:
            f.write(body)
        out_json = gog_json(
            "gmail", "drafts", "create",
            "--to", reply_to,
            "--subject", re_subject,
            "--body-file", body_file,
            "--reply-to-message-id", thread_id,
        )
    finally:
        try:
            os.unlink(body_file)
        except OSError:
            pass

    if out_json is None or out_json == []:
        print("[브레인화 답장 실패] gog drafts create 실패. 큐 상태 유지.")
        return 0
    draft_id = ""
    if isinstance(out_json, dict):
        draft_id = out_json.get("id") or ""

    _attach_draft_to_note(item, draft_id)

    # gtask 합성 (답장할일 케이스)
    gtask_id: str | None = None
    actual_due: str | None = None
    if create_gtask_too:
        actual_due = user_due or _extract_due_from_email(item, now)
        gt_notes = (
            f"From: {frm}\n"
            f"Thread: https://mail.google.com/mail/u/0/#inbox/{thread_id}\n"
            f"Vault: {item.get('note_path', '')}\n"
            f"Draft: {draft_id}"
        )
        gtask_id = create_gtask(state, item.get("subject", "(제목 없음)"),
                                gt_notes, due_date=actual_due)
        if gtask_id:
            _attach_gtask_to_note(item, gtask_id, actual_due)

    safety = _safety_bulk_label(plan)

    ok, ferr = finalize_proceed(item, label=LABEL_PROCEED)
    if not ok:
        print(f"[브레인화 답장] Drafts 등록은 됐으나 라벨/archive 실패: {ferr}")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    state.setdefault("awaiting_reply", []).append({
        "msg_id": thread_id,
        "drafted_at": now.isoformat(timespec="seconds"),
        "draft_id": draft_id,
        "subject": subject,
        "vault_note_path": item.get("note_path", ""),
        "gtask_id": gtask_id,
    })

    para = item.get("proposed_para_path") or "sources/00_inbox/"
    residual = _count_pending_review(plan)
    preview = body[:300] + ("…" if len(body) > 300 else "")
    title = "[브레인화 답장할일 초안]" if create_gtask_too else "[브레인화 답장 초안]"
    gtask_line = ""
    if create_gtask_too:
        if gtask_id:
            due_msg = f" (마감 {actual_due})" if actual_due else " (마감 없음)"
            gtask_line = f"  · Google Tasks 등록 (gtask_id={gtask_id}){due_msg}\n"
        else:
            gtask_line = "  · Google Tasks 등록 실패 (답장 초안만 진행)\n"
    header = (
        safety
        + f"{title} {_short_subject(item.get('subject',''), 40)}\n"
        + f"  · 수신자: {reply_to}\n"
        + f"  · Gmail Drafts 검토 후 발송 (draft_id={draft_id})\n"
        + gtask_line
        + f"  · 노트 위치 {para}, 라벨 '{LABEL_PROCEED}' + archive\n"
        + f"  · 발송 감지 시 다음 cron 폴 사이클에 '{LABEL_DONE}' 으로 자동 promote\n"
        + f"  · 잔여 review 대기 {residual}건\n\n"
        + f"본문 미리보기:\n{preview}\n\n"
    )
    msg_text = _propose_next_or_complete(state, plan, now, header)
    print(msg_text)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def _extract_due_from_email(item: dict, now: dt.datetime) -> str | None:
    """메일 노트의 ## 요약 섹션에서 명시적 마감일 추출 (Haiku 4.5).
    Returns 'YYYY-MM-DD' or None. 비명시적 표현 ('가급적 빨리' 등) 은 None."""
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
    summary = _extract_summary_section(text, max_lines=20)
    if not summary:
        return None

    today_iso = now.date().isoformat()
    prompt = (
        f"오늘 날짜: {today_iso} (KST)\n\n"
        f"다음 메일 요약에서 명시적 마감일·기한·due date 를 한 개만 추출하라.\n"
        f"- 추출 가능 → JSON: {{\"due\": \"YYYY-MM-DD\"}}\n"
        f"- 명시적 마감일 없음 → JSON: {{\"due\": null}}\n"
        f"- '가급적 빨리', '편하실 때' 등 비명시적 표현은 null.\n"
        f"- 상대 표현 (예: '내일', '다음주 금요일') 은 오늘 기준으로 절대 날짜로 변환.\n"
        f"응답은 JSON 객체 한 줄만. 다른 텍스트·코드블록·해설 금지.\n\n"
        f"[메일 제목] {item.get('subject', '')}\n"
        f"[메일 요약]\n{summary[:2000]}\n"
    )
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
    due = parsed.get("due") if isinstance(parsed, dict) else None
    if isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        return due
    return None


def _attach_gtask_to_note(item: dict, gtask_id: str, due_date: str | None) -> None:
    """노트 frontmatter 에 gtask_id / gtask_due 기록. 실패해도 raise 안 함 (best-effort)."""
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
    text = _replace_fm_field(text, "gtask_id", gtask_id)
    if due_date:
        text = _replace_fm_field(text, "gtask_due", due_date)
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


def cmd_gtask(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """현재 review 항목을 Google Tasks 에 등록 + 즉시 완료 (LABEL_DONE).
    Usage: gtask [thread_id] [YYYY-MM-DD] [note...]
        thread_id 생략 시 단일 pending_review 자동 보강.
        YYYY-MM-DD 생략 시 본문에서 LLM 추출, 추출 실패 시 마감 없이 등록 (묻지 않음).
        나머지 토큰들은 자유 메모로 묶여 Google Tasks notes 끝에 'Note: …' 로 append."""
    thread_id: str | None = None
    user_due: str | None = None
    note_extra_tokens: list[str] = []
    for a in argv:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
            user_due = a
        elif thread_id is None and re.fullmatch(r"[0-9a-fA-F]{8,}", a):
            thread_id = a
        else:
            note_extra_tokens.append(a)
    note_extra = " ".join(note_extra_tokens).strip()

    plan = state.get("pending_plan")
    item, err = _resolve_pending_review_thread(plan, thread_id)
    if not item:
        print(f"[브레인화] {err}")
        return 0

    actual_due = user_due or _extract_due_from_email(item, now)

    title = item.get("subject", "(제목 없음)") or "(제목 없음)"
    msg_id = item.get("msg_id", "")
    notes = (
        f"From: {item.get('from', '')}\n"
        f"Thread: https://mail.google.com/mail/u/0/#inbox/{msg_id}\n"
        f"Vault: {item.get('note_path', '')}"
    )
    if note_extra:
        notes += f"\n\nNote: {note_extra}"
    gtask_id = create_gtask(state, title, notes, due_date=actual_due)
    if not gtask_id:
        print("[브레인화 할일 실패] Google Tasks 생성 실패. 큐 상태 유지.")
        return 0

    _attach_gtask_to_note(item, gtask_id, actual_due)

    safety = _safety_bulk_label(plan)

    ok, ferr = finalize_proceed(item, label=LABEL_DONE)
    if not ok:
        print(f"[브레인화 할일] gtask 등록은 됐으나 라벨/archive 실패: {ferr}")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    para = item.get("proposed_para_path") or "sources/00_inbox/"
    residual = _count_pending_review(plan)
    due_msg = f" (마감 {actual_due})" if actual_due else " (마감 없음)"
    note_line = f"  · 메모: {note_extra}\n" if note_extra else ""
    header = (
        safety
        + f"[브레인화 할일] {_short_subject(item.get('subject',''), 40)}{due_msg}\n"
        + f"  · Google Tasks 등록 (gtask_id={gtask_id})\n"
        + note_line
        + f"  · 노트 위치 {para}, 라벨 '{LABEL_DONE}' + archive\n"
        + f"  · 잔여 review 대기 {residual}건\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


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


def _parse_manual_schedule_args(argv: list[str], now: dt.datetime) -> dict | None:
    """수동 인자 파싱: [YYYY-MM-DD] [HH:MM] [summary tokens…].
    Returns dict 또는 None (날짜 토큰 없음 → LLM fallback)."""
    date_str = None
    time_str = None
    rest: list[str] = []
    for a in argv:
        if date_str is None and _DATE_RE.match(a):
            date_str = a
        elif time_str is None and _TIME_RE.match(a):
            time_str = a
        else:
            rest.append(a)
    if not date_str:
        return None
    if time_str:
        start = f"{date_str}T{time_str}:00+09:00"
        try:
            start_dt = dt.datetime.fromisoformat(start.replace("+09:00", ""))
            end_dt = start_dt + dt.timedelta(hours=1)
            end = end_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
        except ValueError:
            end = start
        all_day = False
    else:
        start = date_str
        end = date_str
        all_day = True
    return {
        "summary": " ".join(rest).strip() or None,
        "start": start,
        "end": end,
        "location": "",
        "all_day": all_day,
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
    """Google Calendar primary 에 이벤트 생성. Returns (event_id, error)."""
    description = (
        f"Gmail thread: https://mail.google.com/mail/u/0/#inbox/{thread_id}\n"
        f"Vault note: {note_rel}"
    )
    args = [
        "calendar", "create", "primary",
        "--summary", ev.get("summary") or "(제목 없음)",
        "--from", ev["start"],
        "--to", ev.get("end") or ev["start"],
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


def cmd_schedule(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """현재 review 항목을 Google Calendar 에 일정으로 등록 + 즉시 완료 (LABEL_DONE).
    Usage: schedule [thread_id] [YYYY-MM-DD] [HH:MM] [summary tokens…]
        thread_id 생략 시 단일 pending_review 자동 보강.
        날짜 토큰 생략 시 본문에서 Opus 4.7 추출, 실패 시 안내 후 중단 (큐 유지)."""
    thread_id: str | None = None
    remaining: list[str] = []
    for a in argv:
        if thread_id is None and re.fullmatch(r"[0-9a-fA-F]{8,}", a):
            thread_id = a
        else:
            remaining.append(a)

    plan = state.get("pending_plan")
    item, err = _resolve_pending_review_thread(plan, thread_id)
    if not item:
        print(f"[브레인화] {err}")
        return 0

    ev = _parse_manual_schedule_args(remaining, now)
    if ev is None:
        ev = _extract_schedule_from_email(item, now)
    elif not ev.get("summary"):
        ev["summary"] = item.get("subject", "(제목 없음)") or "(제목 없음)"

    if ev is None:
        print(
            "[브레인화 일정] 본문에서 명확한 일시를 찾지 못했습니다. "
            "수동 인자로 다시 시도: `/g 5 YYYY-MM-DD HH:MM [제목]` (또는 종일이면 시간 생략). "
            "큐는 유지됩니다."
        )
        return 0

    event_id, cerr = _create_calendar_event(ev, item.get("msg_id", ""), item.get("note_path", ""))
    if not event_id:
        print(f"[브레인화 일정 실패] {cerr}\n큐는 유지됩니다.")
        return 0

    _attach_schedule_to_note(item, event_id, ev)
    fl = item.setdefault("followups", [])
    fl.append({
        "kind": "schedule",
        "summary": ev.get("summary", ""),
        "start": ev.get("start", ""),
        "event_id": event_id,
        "at": now.isoformat(),
    })

    safety = _safety_bulk_label(plan)

    ok, ferr = finalize_proceed(item, label=LABEL_DONE)
    if not ok:
        print(f"[브레인화 일정] 이벤트는 등록됐으나 라벨/archive 실패: {ferr}")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    para = item.get("proposed_para_path") or "sources/00_inbox/"
    residual = _count_pending_review(plan)
    when_msg = ev["start"] if ev.get("all_day") else ev["start"]
    header = (
        safety
        + f"[브레인화 일정] {_short_subject(item.get('subject',''), 40)}\n"
        + f"  · Google Calendar 등록 — {ev.get('summary','')} @ {when_msg}"
        + (f", 위치 {ev['location']}" if ev.get("location") else "")
        + (" (종일)" if ev.get("all_day") else "")
        + f" (event_id={event_id})\n"
        + f"  · 노트 위치 {para}, 라벨 '{LABEL_DONE}' + archive\n"
        + f"  · 잔여 review 대기 {residual}건\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def _nl_dispatch_one(
    state: dict, now: dt.datetime, item: dict, action: str, args: dict
) -> int:
    """단일 nl action 을 기존 cmd_* 로 위임. cmd_nl 의 dict/list 분기에서 공통 사용."""
    tid = item.get("msg_id", "")
    if action == "reply":
        instr = (args.get("instruction") or "").strip()
        sub_argv = [tid] + (instr.split() if instr else [])
        return cmd_draft_reply(state, now, sub_argv)
    if action == "reply-task":
        instr = (args.get("instruction") or "").strip()
        due = (args.get("due") or "").strip() or None
        sub_argv = [tid] + (instr.split() if instr else [])
        return cmd_draft_reply(state, now, sub_argv,
                               create_gtask_too=True, user_due=due)
    if action == "task":
        sub_argv = [tid]
        due = (args.get("due") or "").strip()
        if due:
            sub_argv.append(due)
        return cmd_gtask(state, now, sub_argv)
    print(f"[브레인화 nl] 알 수 없는 action: {action}")
    return 0


def cmd_nl(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """자연어 후속조치 dispatcher. Opus 4.7 이 자연어 → 3종 명령(reply/reply-task/task)
    중 하나(또는 다중 의도 시 list) 로 변환 후 기존 cmd_* 로 위임.
    thread_id 가 명시 안 되고 단일 pending_review 항목만 있으면 자동 보강."""
    if not argv:
        print("[브레인화] nl 에는 자연어 문장이 필요합니다.")
        return 1

    plan = state.get("pending_plan")
    if not plan:
        print("[브레인화] pending plan 이 없습니다.")
        return 0

    # 첫 토큰이 plan 의 thread_id 와 매칭하면 명시, 아니면 자동 보강
    item: dict | None = None
    natural_text = ""
    explicit = _find_item_by_thread(plan, argv[0])
    if explicit:
        item = explicit
        natural_text = " ".join(argv[1:]).strip()
    else:
        natural_text = " ".join(argv).strip()

    if item is None:
        candidates = [
            it for it in _proceed_items(plan)
            if it.get("confirm_status") == "pending_review"
        ]
        if len(candidates) == 0:
            print("[브레인화] 현재 review 대기 항목이 없습니다.")
            return 0
        if len(candidates) > 1:
            tids = ", ".join(it.get("msg_id", "") for it in candidates)
            print(f"[브레인화] review 대기 항목이 여러 개입니다 ({tids}) "
                  f"— thread_id 를 명시하세요.")
            return 0
        item = candidates[0]
    else:
        if (item.get("category") != "proceed"
                or item.get("confirm_status") != "pending_review"):
            print(f"[브레인화] thread_id '{item.get('msg_id')}' "
                  f"가 review 대기 상태가 아닙니다.")
            return 0

    if not natural_text:
        print("[브레인화] 자연어 문장이 비어 있습니다.")
        return 1

    today_str = (
        f"{now.date().isoformat()} "
        f"({DOW_KR[now.date().weekday()]}) {now.strftime('%H:%M')} KST"
    )

    spec = (
        "사용 가능한 명령 3가지 — 자연어 지시를 JSON 으로 변환:\n\n"
        "1. reply — Gmail 회신 초안 작성 (Drafts 등록 + awaiting_reply 큐, 발송은 사용자가)\n"
        '   args: { "instruction": "<회신 톤·요지를 한 문장으로>" }\n\n'
        "2. reply-task — reply + Google Tasks 등록 합성 (마감 있는 답장)\n"
        '   args: { "instruction": "<회신 지시>", "due": "YYYY-MM-DD (선택)" }\n'
        "   본문에 마감일 명시되어 있으면 절대 날짜로 추출. 없으면 due 키 생략 (마감 없이 등록).\n\n"
        "3. task — Google Tasks 만 등록 (답장 없이 할일만)\n"
        '   args: { "due": "YYYY-MM-DD (선택)" }\n\n'
        "응답 형식 (JSON only, 다른 텍스트·코드블록·해설 금지):\n"
        " - 단일 의도 → object: "
        '{ "action": "reply|reply-task|task", "args": { ... } }\n'
        " - 다중 의도 → array of object\n\n"
        "해석 불가능하거나 3종 외 의도이면:\n"
        '{ "action": "error", "reason": "<짧은 한국어 설명>" }\n\n'
        "예시:\n"
        '- "회신 초안 정중하게 거절" → '
        '{"action":"reply","args":{"instruction":"정중하게 거절"}}\n'
        '- "5월 15일까지 답장하고 할일도 등록" → '
        '{"action":"reply-task","args":{"instruction":"답장","due":"2026-05-15"}}\n'
        '- "내일까지 자료 확인하라는 할일만" → '
        '{"action":"task","args":{"due":"<내일 날짜>"}}\n'
    )

    prompt = (
        f"오늘 일시 (KST): {today_str}\n"
        f"이메일 제목 컨텍스트: {item.get('subject','')}\n\n"
        f"{spec}\n"
        f"자연어 지시:\n{natural_text}\n"
    )

    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-opus-4-7",
        "--disallowedTools",
        "Bash,Read,Edit,Write,Glob,Grep,Agent,WebFetch,WebSearch",
    ]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("[브레인화 nl 실패] LLM timeout. 큐 상태 유지.")
        return 0
    if r.returncode != 0:
        print(f"[브레인화 nl 실패] LLM exit={r.returncode}.")
        return 0

    out = (r.stdout or "").strip()
    if out.startswith("```"):
        nl_idx = out.find("\n")
        out = out[nl_idx + 1:] if nl_idx >= 0 else out[3:]
        if out.rstrip().endswith("```"):
            out = out.rstrip()[:-3]
        out = out.strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        # object 또는 array 어느 쪽이든 가능 — 가장 바깥 괄호 쌍을 찾아 재시도
        snippet = None
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            try:
                s = out.index(open_ch)
                e = out.rindex(close_ch) + 1
            except ValueError:
                continue
            try:
                parsed = json.loads(out[s:e])
                snippet = out[s:e]
                break
            except json.JSONDecodeError:
                continue
        if snippet is None:
            print(f"[브레인화 nl 실패] LLM 응답 JSON 파싱 실패: {out[:300]}")
            return 0

    # dict (단일 의도) / list (다중 의도) 양쪽 지원
    if isinstance(parsed, dict):
        entries = [parsed]
    elif isinstance(parsed, list):
        entries = [p for p in parsed if isinstance(p, dict)]
        if not entries:
            print(f"[브레인화 nl 실패] LLM 응답이 빈 list: {out[:300]}")
            return 0
    else:
        print(f"[브레인화 nl 실패] 예상치 못한 응답 형식: {type(parsed).__name__}")
        return 0

    last_rc = 0
    for entry in entries:
        action = entry.get("action", "")
        args = entry.get("args") or {}
        if action == "error":
            print(f"[브레인화 nl] 해석 실패: {entry.get('reason','(이유 없음)')}")
            return 0
        last_rc = _nl_dispatch_one(state, now, item, action, args)
        if last_rc != 0:
            return last_rc
    return last_rc


def cmd_cancel(state: dict, now: dt.datetime) -> int:
    """plan 폐기. propose 단계까지 작성된 미확정 노트 파일은 정리한다."""
    plan = state.get("pending_plan")
    cleaned = 0
    if plan:
        for it in _proceed_items(plan):
            if it.get("confirm_status") == "pending_review" and it.get("note_path"):
                p = VAULT_ROOT / it["note_path"]
                try:
                    p.unlink(missing_ok=True)
                    cleaned += 1
                except OSError:
                    pass
    had_plan = bool(plan)
    state["pending_plan"] = None
    state["last_checked"] = now.isoformat()
    save_state(state)
    if had_plan:
        suffix = f" 미확정 노트 {cleaned}건 정리됨." if cleaned else ""
        print(f"[브레인화] pending plan 폐기. 다음 폴링에서 재분류됩니다.{suffix}")
    else:
        print(f"[브레인화] 폐기할 plan 이 없습니다.")
    return 0


def cmd_snooze(state: dict, now: dt.datetime, minutes: int) -> int:
    if minutes <= 0:
        print("[브레인화] snooze 분 단위는 양수여야 합니다.", file=sys.stderr)
        return 1
    until = now + dt.timedelta(minutes=minutes)
    state["snooze_until"] = until.isoformat()
    state["last_checked"] = now.isoformat()
    save_state(state)
    print(f"[브레인화] {minutes}분 발화 보류. {until.strftime('%H:%M')} 까지.")
    return 0


def cmd_migrate_inbox(state: dict, now: dt.datetime, apply: bool) -> int:
    """sources/00_inbox/ 의 기존 brainify 노트들을 frontmatter proposed_para_path 기준으로
    knowledge/<PARA>/ + sources/<PARA>/ 로 일괄 이동. 기본 dry-run, --apply 시 실제 수행."""
    inbox = VAULT_INBOX
    if not inbox.is_dir():
        print("[migrate] sources/00_inbox/ 가 없습니다.")
        return 0

    movable: list[dict] = []
    skipped: list[dict] = []
    for note_path in sorted(inbox.glob("*.md")):
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError as e:
            skipped.append({"note": note_path.name, "reason": f"read fail: {e}"})
            continue
        fm = _parse_frontmatter(text)
        para_raw = fm.get("proposed_para_path") or ""
        if isinstance(para_raw, list):
            para_raw = para_raw[0] if para_raw else ""
        para = _normalize_para_coord(str(para_raw))
        if not para:
            skipped.append({
                "note": note_path.name,
                "reason": "proposed_para_path 없음/staging",
            })
            continue

        sources_raw = fm.get("sources") or []
        if isinstance(sources_raw, str):
            sources_raw = [sources_raw]
        attach_paths: list[pathlib.Path] = []
        for s in sources_raw:
            p = VAULT_ROOT / s
            if p.exists() and p not in attach_paths:
                attach_paths.append(p)
        # gmail_threadIds 기반으로 _attachments/<tid>/ 도 탐색 (sources 누락된 경우)
        gmail_tids = fm.get("gmail_threadIds") or []
        if isinstance(gmail_tids, str):
            gmail_tids = [gmail_tids]
        primary_tid = ""
        for tid in gmail_tids:
            if not primary_tid:
                primary_tid = tid
            tdir = VAULT_ATTACH_ROOT / tid
            if tdir.is_dir():
                for f in sorted(tdir.iterdir()):
                    if f.is_file() and not f.name.startswith(".") and f not in attach_paths:
                        attach_paths.append(f)

        attach_rel_all = [str(p.relative_to(VAULT_ROOT)) for p in attach_paths]
        kept_preview, duped_preview = _dedupe_attachments(attach_rel_all)
        movable.append({
            "note": str(note_path.relative_to(VAULT_ROOT)),
            "para": para,
            "attachments": attach_rel_all,
            "kept": kept_preview,
            "duped": duped_preview,
            "msg_id": primary_tid,
        })

    L: list[str] = []
    L.append(
        f"[브레인화 마이그레이션 — {'APPLY' if apply else 'DRY RUN'}] "
        f"{fmt_date_kr(now.date())} {now.strftime('%H:%M')}"
    )
    L.append("")
    L.append(f"이동 대상: {len(movable)}건  /  건너뛰기: {len(skipped)}건")
    L.append("")
    for a in movable:
        L.append(f"  • {a['note']}")
        L.append(f"      → 노트: knowledge/{a['para']}/")
        kept = a.get("kept") or []
        duped = a.get("duped") or []
        if kept or duped:
            note = f"{len(kept)}건"
            if duped:
                note += f" (+ 중복 {len(duped)}건은 _attachments/_dup/ 로 격리)"
            L.append(f"      → 첨부: sources/{a['para']}/  {note}")
            for ap in kept[:3]:
                L.append(f"          - {ap}")
            if len(kept) > 3:
                L.append(f"          - 외 {len(kept) - 3}건")
    if skipped:
        L.append("")
        L.append("건너뛴 항목:")
        for s in skipped:
            L.append(f"  • {s['note']}  — {s['reason']}")
    L.append("")

    if not apply:
        L.append("[명령]")
        L.append("  /gws-assistant migrate-inbox --apply  — 위 계획대로 실제 이동 수행")
        print("\n".join(L))
        return 0

    # 실제 이동
    ok_n = 0
    fail_n = 0
    errs: list[str] = []
    for a in movable:
        fake_item = {
            "note_path": a["note"],
            "attachments": a["attachments"],
            "msg_id": a["msg_id"],
        }
        ok, err = _relocate_to_para(fake_item, a["para"])
        if ok:
            ok_n += 1
        else:
            fail_n += 1
            errs.append(f"  - {a['note']}: {err}")
    L.append(f"실행 결과: 성공 {ok_n}건, 실패 {fail_n}건")
    for e in errs[:10]:
        L.append(e)
    print("\n".join(L))
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


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


def cmd_migrate_brainify_labels(state: dict, now: dt.datetime, apply: bool) -> int:
    """Legacy 'brainify/proceed' (= LABEL_PROCEED) 라벨 메일을 일괄 'brainify/done' 으로 promote.
    구 모델 (2단계 종결 — 진행/완료 구분) 잔존 메일들을 신 모델 (1단계 종결 + awaiting_reply 폴링)
    로 정리하기 위한 1회성 마이그레이션. dry-run 기본, --apply 시 실제 수행."""
    query = f"label:{LABEL_PROCEED}"
    items = gog_json("gmail", "search", query, "--max", "500")
    if items is None:
        print("[migrate-brainify-labels] gmail search 실패 (gog OAuth?)", file=sys.stderr)
        return 1
    if isinstance(items, dict):
        items = items.get("messages", items.get("items", []))
    items = items or []

    # 현재 awaiting_reply 큐의 thread_id 는 제외 — 발송 대기 중인 항목은 건드리면 안 됨.
    awaiting_ids = {e.get("msg_id") for e in (state.get("awaiting_reply") or [])}
    targets = [m for m in items if m.get("id") not in awaiting_ids]
    skipped = len(items) - len(targets)

    if not targets:
        print(f"[migrate-brainify-labels] '{LABEL_PROCEED}' 라벨 메일 0건 (awaiting_reply 제외 {skipped}건). 정리 완료.")
        return 0

    L = [
        f"[migrate-brainify-labels] '{LABEL_PROCEED}' → '{LABEL_DONE}' 일괄 promote",
        f"  대상 {len(targets)}건 (awaiting_reply 큐 제외 {skipped}건)",
        "",
    ]
    for i, m in enumerate(targets[:20], 1):
        mid = m.get("id", "")
        subj = (m.get("subject", "") or "(제목 없음)")[:60]
        date = m.get("date", "")
        L.append(f"  {i}. [{mid[:12]}…] {date}  {subj}")
    if len(targets) > 20:
        L.append(f"  … 외 {len(targets) - 20}건")
    L.append("")

    if not apply:
        L.append("[명령] /gws-assistant migrate-brainify-labels --apply  — 위 목록 실제 promote")
        print("\n".join(L))
        return 0

    ok_n = 0
    fail_n = 0
    for m in targets:
        mid = m.get("id", "")
        if not mid:
            fail_n += 1
            continue
        ok, _err = gog_call(
            "gmail", "labels", "modify", mid,
            "--add", LABEL_DONE,
            "--remove", LABEL_PROCEED,
        )
        if ok:
            ok_n += 1
        else:
            fail_n += 1
    L.append(f"실행 결과: 성공 {ok_n}건, 실패 {fail_n}건")
    print("\n".join(L))
    return 0


# ============================================================================
# Poll (default mode)
# ============================================================================

def _poll_awaiting_replies(state: dict, now: dt.datetime) -> str:
    """awaiting_reply 큐의 각 항목에 대해 발송 감지 (drafted_at 이후 SENT 라벨 메시지).
    감지 시: LABEL_PROCEED → LABEL_DONE promote + 노트 frontmatter 에 replied_at 기록 + 큐에서 제거.
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
        ok, _err = gog_call(
            "gmail", "labels", "modify", thread_id,
            "--add", LABEL_DONE,
            "--remove", LABEL_PROCEED,
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
    lines = [f"[발송 감지 → 자동 종결] {len(promoted)}건 ('{LABEL_PROCEED}' → '{LABEL_DONE}')"]
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


def _run_label_drain(state: dict, now: dt.datetime, *,
                     label: str, tag: str,
                     extra_action=None,
                     dry_run: bool = False,
                     limit: int = GMAIL_SAVE_MAX) -> tuple[str, str]:
    """§11 액션-라벨 완전무인 드레인 코어 (1~8 공용).
    label   : 트리거·제거 라벨 (예 '1 저장', '2 일정')
    tag     : 보고 머리표
    extra_action: 신규·재개 경로에서 노트 staging 직후·PARA 이동 전에 실행할
                  콜백 (item, now)->(ok, err). None 이면 없음.
                  **idempotent 필수** — 크래시 후 재개 시 재호출되므로
                  (예: 이미 만든 캘린더 이벤트 재생성 금지).
    반환 (full, problem): full=전체요약(터미널), problem=오류만(cron→Telegram).
    `-label:"9 완료"` 미포함 — commit 부분실패 stuck 도 가드로 자가치유.
    Telegram 정책: 성공 침묵, 오류만. dry_run=계획만 (problem 항상 "")."""
    query = f'label:"{label}"'
    results = gog_json("gmail", "search", query, "--max", str(limit))
    if results is None:
        _m = f"[{tag} 드레인] gmail 검색 실패 (gog OAuth?)\n"
        return (_m, _m)
    if isinstance(results, dict):
        results = results.get("messages", results.get("items", []))
    if not results:
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

        # ── threadId 멱등 가드 ──
        existing, relocated = _existing_note_for_thread(tid)
        if existing is not None and relocated:
            # 노트+이동 끝 (extra_action 도 끝남), 라벨만 미완 → commit 복구
            if dry_run:
                planned.append(f"· {short} → [복구] 라벨 commit 만")
                continue
            ok, err = _commit_action_label(tid, label)
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
            ok, err = _commit_action_label(tid, label)
            (done if ok else errors).append(
                short if ok else f"{short} (commit 실패: {err})")
            continue

        # ── 신규 (PHI 점검 없음) ──
        if dry_run:
            planned.append(f"· {short} → [신규] 노트 생성+배치+commit")
            continue

        # ── 본문 fetch + 노트 생성 + staging write ──
        ok, err = propose_proceed(item)
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
        ok, err = _commit_action_label(tid, label)
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


def cmd_poll(state: dict, now: dt.datetime, force: bool) -> int:
    """1. awaiting_reply 큐 발송 감지 → LABEL_PROCEED → LABEL_DONE 자동 promote (게이트 무관).
       2. Gates → 통과해야 신규 메일 발화.
       3. Snooze → 활성 시 신규 발화 침묵 (감지 메시지는 출력).
       4. fetch inbox pending → classify → merge into pending plan.
       5. plan 의 msg_id set 가 마지막 발화 set 와 다르면 발화."""
    awaiting_msg = _poll_awaiting_replies(state, now)

    # §11 액션-라벨 완전무인 드레인 — 게이트 무관 (백그라운드).
    # 단일 킬스위치 state['autodrain_enabled'] (기본 False) 가 1~8 핸들러
    # 전부를 관장 (2026-05-16 Dr. Ben: 라벨별 플래그 대신 단일 — 인지부하 최소·현실 부합).
    # Telegram: 성공 완전 침묵, 오류 시에만 발화 (일일 다이제스트 없음).
    # 핸들러 계약: (state, now) -> (full, problem). 2~8 은 §11.5 에서
    # 아래 튜플에 append (동일 게이트·동일 problem 누적).
    if state.get("autodrain_enabled"):
        _problems: list[str] = []
        for _handler in (_run_save_drain, _run_schedule_drain):  # 1 저장·2 일정. 추후 3~8.
            _full, _problem = _handler(state, now)
            if _problem:
                _problems.append(_problem)
        if _problems:
            _pm = "".join(_problems)
            awaiting_msg = (_pm + awaiting_msg) if awaiting_msg else _pm

    if not force:
        gate_fail = check_gates(now)
        if gate_fail:
            if awaiting_msg:
                print(awaiting_msg)
            state["last_checked"] = now.isoformat()
            save_state(state)
            return 0

        if is_snoozed(state, now):
            if awaiting_msg:
                print(awaiting_msg)
            state["last_checked"] = now.isoformat()
            save_state(state)
            return 0

    emails = fetch_inbox_pending()
    if emails is None:
        print("[gws-assistant] gmail fetch 실패 (gog OAuth?)", file=sys.stderr)
        emails = []

    plan = merge_plan(state, emails, now)

    current_ids = {it["msg_id"] for it in plan["items"] if it.get("msg_id")}
    last_announced = set(plan.get("last_announced_msg_ids", []))

    new_in_plan = current_ids - last_announced
    removed = last_announced - current_ids

    # 발화 조건: 신규 msg 가 plan 에 추가됨 OR plan 이 비었다 (외부 처리 완료 상태 보고)
    should_announce = bool(new_in_plan) or (force and current_ids)

    if not should_announce:
        # 변경 사항만 plan 에 반영해두고 침묵 (단, awaiting_msg 가 있으면 출력)
        if removed:
            # 외부에서 처리된 항목 반영 — last_announced 도 정리
            plan["last_announced_msg_ids"] = sorted(current_ids)
        state["pending_plan"] = plan if current_ids else None
        state["last_checked"] = now.isoformat()
        save_state(state)
        if awaiting_msg:
            print(awaiting_msg)
        return 0

    msg = format_plan_message(now, plan, new_count=len(new_in_plan), total_count=len(current_ids))
    if awaiting_msg:
        print(awaiting_msg + msg)
    else:
        print(msg)

    plan["last_announced_msg_ids"] = sorted(current_ids)
    state["pending_plan"] = plan if current_ids else None
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def cmd_pending_review(state: dict, now: dt.datetime, batch_size: int = GMAIL_MAX) -> int:
    """'브레인화/보류' 라벨 메일을 batch_size 건 가져와 라벨 제거 후 일반 plan flow 로
    재분류·재처리. 사용자가 외부 작업/숙고 후 보류 박스를 정리할 때 진입점.
    활성 plan 이 있으면 거부 — 폴링 plan 과 보류 정리 plan 의 혼선을 막기 위함."""
    if state.get("pending_plan"):
        print("[브레인화 보류 정리] 활성 plan 이 있습니다 — 먼저 처리 또는 /g 취소 후 재시도하세요.")
        return 0

    mails = fetch_pending_labeled(batch_size)
    if mails is None:
        print("[브레인화 보류 정리] gmail fetch 실패 (gog OAuth?)", file=sys.stderr)
        return 1
    if not mails:
        print("[브레인화 보류 정리] 보류 라벨 메일이 없습니다. 정리 완료.")
        return 0

    cleaned: list[dict] = []
    fail_count = 0
    for m in mails:
        mid = m.get("id") or ""
        if not mid:
            fail_count += 1
            continue
        ok, _err = gog_call("gmail", "labels", "modify", mid, "--remove", LABEL_PENDING)
        if ok:
            cleaned.append(m)
        else:
            fail_count += 1

    if not cleaned:
        print(f"[브레인화 보류 정리] 라벨 제거 모두 실패 ({fail_count}건). 정리 중단.")
        return 0

    plan = merge_plan(state, cleaned, now)
    current_ids = sorted({it["msg_id"] for it in plan.get("items", []) if it.get("msg_id")})
    plan["last_announced_msg_ids"] = current_ids

    state["pending_plan"] = plan if current_ids else None
    state["last_checked"] = now.isoformat()
    save_state(state)

    head_lines = [f"[브레인화 보류 정리] {len(cleaned)}건 라벨 제거 후 재분류했습니다."]
    if fail_count:
        head_lines.append(f"  · 라벨 제거 실패 {fail_count}건 스킵")
    head_lines.append("")
    head = "\n".join(head_lines)

    msg = format_plan_message(
        now, plan, new_count=len(current_ids), total_count=len(current_ids),
    )
    print(head + msg)
    return 0


# ============================================================================
# Main / arg parsing
# ============================================================================

# ============================================================================
# Correction log — 사용자 피드백을 vault gmail-capture.md 에 누적,
# 다음 LLM 분류 시 프롬프트로 자동 주입되어 자기진화.
# ============================================================================

def _append_correction_log(now: dt.datetime, frm: str, subject: str,
                            old_cat: str, new_cat: str, memo: str) -> bool:
    """vault gmail-capture.md 의 교정 로그 marker 사이에 한 줄 append. True on success."""
    safe_subject = (subject or "").replace("\n", " ").replace("\r", "").strip()[:80]
    safe_from = (frm or "").replace("\n", " ").strip()[:80]
    line = (f"- {now.strftime('%Y-%m-%d')}: from={safe_from}, "
            f"subject=\"{safe_subject}\": {old_cat or '?'}→{new_cat}")
    if memo:
        line += f" ({memo})"
    try:
        text = VAULT_CAPTURE_DOC.read_text(encoding="utf-8")
    except OSError:
        return False
    start_marker = "<!-- correction-log-start -->"
    end_marker = "<!-- correction-log-end -->"
    s = text.find(start_marker)
    e = text.find(end_marker)
    if s < 0 or e < 0 or s > e:
        return False
    s_end = s + len(start_marker)
    inner = text[s_end:e].rstrip()
    new_inner = inner + "\n" + line + "\n"
    new_text = text[:s_end] + new_inner + text[e:]
    try:
        VAULT_CAPTURE_DOC.write_text(new_text, encoding="utf-8")
    except OSError:
        return False
    return True


def cmd_reclassify(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """현재 plan 안의 item 의 category 만 in-place 로 변경.
    Gmail 라벨은 안 건드림 — 다음 approve 시 새 category 로 처리되어
    proceed 면 propose+note 흐름까지 정상적으로 돌아감.
    plan 외 메일은 cmd_correct 를 쓸 것."""
    if len(argv) < 2:
        print("[브레인화 재분류] thread_id 와 new_category 필요.")
        return 1
    tid = argv[0]
    new_cat = argv[1].lower()
    memo = " ".join(argv[2:]).strip() if len(argv) > 2 else ""

    if new_cat not in VALID_CATEGORIES:
        print(f"[브레인화 재분류] 알 수 없는 카테고리 '{new_cat}'.")
        return 1

    plan = state.get("pending_plan")
    if not plan:
        print("[브레인화 재분류] 활성 plan 없음. 먼저 폴링 발화 필요.")
        return 1

    item = _find_item_by_thread(plan, tid)
    if not item:
        print(f"[브레인화 재분류] thread_id '{tid}' 가 현재 plan 에 없습니다.")
        print("    plan 외 후처리 교정은 'correct' 명령 사용.")
        return 1

    old_cat = item.get("category")
    if old_cat == new_cat:
        print(f"[브레인화 재분류] 이미 '{new_cat}' 입니다. 변경 없음.")
        return 0

    item["category"] = new_cat
    item["action"] = make_action(new_cat)
    item["reason"] = (f"사용자 재분류: {old_cat}→{new_cat}"
                      + (f" ({memo})" if memo else ""))
    if new_cat == "proceed":
        item["confirm_status"] = "pending_review"
        item.setdefault("note_path", None)
        item.setdefault("proposed_para_path", None)
        item.setdefault("proposed_links", [])
        item.setdefault("body_summary", "")
        item.setdefault("attachments", [])
    else:
        item["confirm_status"] = None

    plan["items"] = sort_plan_items(plan.get("items", []))
    save_state(state)

    log_ok = _append_correction_log(
        now, item.get("from", ""), item.get("subject", ""),
        old_cat, new_cat, memo or "in-plan reclassify"
    )

    out = [
        f"[브레인화 재분류] {_short_subject(item.get('subject',''), 50)}",
        f"  → category {old_cat} → {new_cat} (plan 내부, Gmail 라벨은 아직 변화 없음)",
        f"  → 다음 /g 맞아 (approve) 시 새 category 로 처리됨",
    ]
    if new_cat == "proceed":
        out.append(f"  → approve 후 propose 큐에 들어가 1건 검토 대상이 됨")
    if log_ok:
        out.append("  → vault 교정 로그에도 기록 (학습 데이터)")
    if memo:
        out.append(f"  → 메모: {memo}")
    print("\n".join(out))
    return 0


def cmd_bulk_reclassify(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """plan 내 in-place 일괄 재분류. argv 는 '<thread_id>:<new_cat>' 토큰 list.
    예: bulk-reclassify 19c5..ab:noise 19c5..cd:noise 19c5..ef:pending
    한 번의 명령으로 다수 항목을 처리하고 결과 plan 을 한 번만 재출력 — 사용자가
    plan 을 보면서 카테고리 위치를 통째로 정리할 때 사용. (Gmail 라벨은 안 건드림.)"""
    plan = state.get("pending_plan")
    if not plan:
        print("[브레인화 일괄 재분류] 활성 plan 없음. 먼저 폴링 발화 필요.")
        return 1
    if not argv:
        print("[브레인화 일괄 재분류] '<thread_id>:<new_cat>' 형식 토큰이 1개 이상 필요합니다.")
        return 1

    summary: list[str] = []
    failed: list[str] = []
    log_count = 0
    for token in argv:
        if ":" not in token:
            failed.append(f"{token}: 형식 오류 (필요: <thread_id>:<new_cat>)")
            continue
        tid, new_cat = token.split(":", 1)
        new_cat = new_cat.lower().strip()
        if new_cat not in VALID_CATEGORIES:
            failed.append(f"{tid}: 알 수 없는 카테고리 '{new_cat}'")
            continue
        item = _find_item_by_thread(plan, tid)
        if not item:
            failed.append(f"{tid}: plan 에 없음")
            continue
        old_cat = item.get("category")
        if old_cat == new_cat:
            continue
        item["category"] = new_cat
        item["action"] = make_action(new_cat)
        item["reason"] = f"사용자 일괄 재분류: {old_cat}→{new_cat}"
        if new_cat == "proceed":
            item["confirm_status"] = "pending_review"
            item.setdefault("note_path", None)
            item.setdefault("proposed_para_path", None)
            item.setdefault("proposed_links", [])
            item.setdefault("body_summary", "")
            item.setdefault("attachments", [])
        else:
            item["confirm_status"] = None
        summary.append(f"{tid[:8]}…: {old_cat}→{new_cat}")
        if _append_correction_log(
            now, item.get("from", ""), item.get("subject", ""),
            old_cat, new_cat, "bulk reclassify",
        ):
            log_count += 1

    plan["items"] = sort_plan_items(plan.get("items", []))
    state["last_checked"] = now.isoformat()
    save_state(state)

    out: list[str] = []
    if summary:
        out.append(f"[브레인화 일괄 재분류] {len(summary)}건 적용:")
        for s in summary:
            out.append(f"  · {s}")
        if log_count:
            out.append(f"  → vault 교정 로그 {log_count}건 기록 (학습 데이터)")
        out.append("")
    if failed:
        out.append(f"[실패 {len(failed)}건]")
        for f in failed:
            out.append(f"  · {f}")
        out.append("")
    if not summary and not failed:
        out.append("[브레인화 일괄 재분류] 변경 없음 (모두 이미 같은 카테고리).")
        out.append("")

    items = plan.get("items", [])
    # new_count=total_count 으로 두어 "(신규 N건 추가)" 노이즈 헤더 회피 — 재분류는 신규 메일 아님.
    out.append(format_plan_message(
        now, plan, new_count=len(items), total_count=len(items),
    ))
    print("\n".join(out))
    return 0


def cmd_correct(state: dict, now: dt.datetime, argv: list[str]) -> int:
    """수동 교정. 라벨 재적용 + vault 교정 로그 append.
    argv: [thread_id, new_category, memo_words...]"""
    if len(argv) < 2:
        print("[브레인화] correct 명령: thread_id 와 new_category 가 필요합니다.")
        print("사용법: /gws-assistant correct <thread_id> <proceed|pending|noise> [메모]")
        return 1

    thread_id = argv[0]
    new_cat = argv[1].lower()
    memo = " ".join(argv[2:]).strip() if len(argv) > 2 else ""

    if new_cat not in VALID_CATEGORIES:
        print(f"[브레인화] 알 수 없는 카테고리 '{new_cat}'. "
              f"proceed|pending|noise 중 하나여야 함.")
        return 1

    thread_data = gog_json("gmail", "thread", "get", thread_id)
    if not thread_data:
        print(f"[브레인화 교정] thread '{thread_id}' fetch 실패 "
              f"(id 잘못됐거나 권한 없음).")
        return 1

    msgs = (thread_data.get("thread") or {}).get("messages") or thread_data.get("messages") or []
    if not msgs:
        print(f"[브레인화 교정] thread '{thread_id}' 에 messages 없음.")
        return 1
    first = msgs[0]
    headers = {(h.get("name") or "").lower(): (h.get("value") or "")
               for h in (first.get("payload") or {}).get("headers") or []}
    frm = headers.get("from", "")
    subject = headers.get("subject", "")

    label_name = LABEL_PROCEED if new_cat == "proceed" else (
        LABEL_PENDING if new_cat == "pending" else LABEL_NOISE
    )
    args = ["gmail", "labels", "modify", thread_id, "--add", label_name]
    for other in [LABEL_PROCEED, LABEL_PENDING, LABEL_NOISE]:
        if other != label_name:
            args.extend(["--remove", other])
    if new_cat in ("proceed", "noise"):
        args.extend(["--remove", "INBOX"])

    ok, err = gog_call(*args)
    if not ok:
        print(f"[브레인화 교정 실패] 라벨 적용 실패: {err}")
        return 1

    log_ok = _append_correction_log(now, frm, subject, "?", new_cat, memo)

    out = [
        f"[브레인화 교정] {_short_subject(subject, 50)}",
        f"  → 라벨 '{label_name}'" + (" + archive" if new_cat in ("proceed", "noise")
                                       else " (inbox 유지)"),
    ]
    if log_ok:
        out.append("  → vault 교정 로그에 기록 — 다음 LLM 분류부터 학습 컨텍스트로 주입됨")
    else:
        out.append("  ⚠ vault 교정 로그 기록 실패 (marker 누락?)")
    if memo:
        out.append(f"  → 메모: {memo}")
    print("\n".join(out))

    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


_LEARN_RULES_LINE_RE = re.compile(
    r"^- \d{4}-\d{2}-\d{2}: from=(.+?),\s+subject=\"[^\"]*\":\s*[^→]*→(\w+)"
)
_EMAIL_ADDR_RE = re.compile(r"<([^>]+)>")


def cmd_learn_rules(state: dict, now: dt.datetime, threshold: int = 2) -> int:
    """vault 교정 로그를 분석해 발신자별 deterministic 규칙 자동 추출.
    같은 발신자 주소가 threshold 회 이상 같은 카테고리로 교정됐으면 등록.
    여러 카테고리로 갈리면 (갈등) 등록 보류."""
    try:
        text = VAULT_CAPTURE_DOC.read_text(encoding="utf-8")
    except OSError as e:
        print(f"[learn-rules] vault 읽기 실패: {e}")
        return 1
    s = text.find("<!-- correction-log-start -->")
    e_idx = text.find("<!-- correction-log-end -->")
    if s < 0 or e_idx < 0 or s > e_idx:
        print("[learn-rules] 교정 로그 marker 없음.")
        return 1
    log_text = text[s + len("<!-- correction-log-start -->"): e_idx]

    counts: dict[str, dict[str, int]] = {}
    total_lines = 0
    for line in log_text.splitlines():
        m = _LEARN_RULES_LINE_RE.match(line.strip())
        if not m:
            continue
        total_lines += 1
        frm_raw = m.group(1).strip()
        cat = m.group(2).strip().lower()
        em = _EMAIL_ADDR_RE.search(frm_raw)
        addr = em.group(1).lower() if em else frm_raw.strip().strip("\"'").lower()
        if not addr or cat not in VALID_CATEGORIES:
            continue
        counts.setdefault(addr, {})
        counts[addr][cat] = counts[addr].get(cat, 0) + 1

    rules = load_deterministic_rules()
    promoted: list[tuple[str, str, int]] = []
    skipped: list[tuple[str, str]] = []
    for addr, cat_counts in counts.items():
        if len(cat_counts) > 1:
            skipped.append((addr, f"갈등 ({cat_counts})"))
            continue
        cat, n = next(iter(cat_counts.items()))
        if n < threshold:
            skipped.append((addr, f"{cat} {n}회 (threshold {threshold} 미달)"))
            continue
        existing = rules.get(addr)
        if existing and existing.get("category") == cat and existing.get("source_corrections", 0) >= n:
            continue
        rules[addr] = {
            "category": cat,
            "added_at": now.isoformat(),
            "source_corrections": n,
        }
        promoted.append((addr, cat, n))

    save_deterministic_rules(rules)

    L = [
        f"[learn-rules] 교정 로그 분석 — {total_lines}개 entry, threshold {threshold}회.",
    ]
    if promoted:
        L.append(f"  새/갱신된 규칙 {len(promoted)}개:")
        for addr, cat, n in promoted:
            L.append(f"    + {addr} → {cat} ({n}회 누적)")
    else:
        L.append("  새 규칙 없음 (이미 모두 반영됨).")
    if skipped:
        L.append(f"  대기 {len(skipped)}개 (threshold 미달 또는 갈등):")
        for addr, why in skipped[:10]:
            L.append(f"    - {addr}: {why}")
        if len(skipped) > 10:
            L.append(f"    ... 외 {len(skipped) - 10}개")
    L.append(f"  활성 규칙 총 {len(rules)}개 ({DETERMINISTIC_RULES_PATH})")
    print("\n".join(L))
    return 0


def cmd_show_rules(state: dict, now: dt.datetime) -> int:
    """현재 활성 deterministic 규칙 표시."""
    rules = load_deterministic_rules()
    if not rules:
        print("[show-rules] 활성 deterministic 규칙 없음. /gws-assistant learn-rules 로 교정 로그에서 학습.")
        return 0
    L = [f"[show-rules] 활성 deterministic 규칙 {len(rules)}개:"]
    for addr, info in sorted(rules.items()):
        cat = info.get("category", "?")
        n = info.get("source_corrections", "?")
        added = info.get("added_at", "")[:10]
        L.append(f"  {addr} → {cat} ({n}회, {added})")
    L.append(f"\n파일: {DETERMINISTIC_RULES_PATH}")
    print("\n".join(L))
    return 0


def main(argv: list[str]) -> int:
    state = load_state()
    now = now_kst()

    # subcommand parsing
    if argv:
        first = argv[0]
        if first == "approve":
            return cmd_approve(state, now)
        if first == "cancel":
            return cmd_cancel(state, now)
        if first == "correct":
            return cmd_correct(state, now, argv[1:])
        if first == "reclassify":
            return cmd_reclassify(state, now, argv[1:])
        if first == "learn-rules":
            return cmd_learn_rules(state, now)
        if first == "show-rules":
            return cmd_show_rules(state, now)
        if first == "snooze":
            try:
                minutes = int(argv[1]) if len(argv) >= 2 else 60
            except ValueError:
                print("[브레인화] snooze 인자는 정수(분).", file=sys.stderr)
                return 1
            return cmd_snooze(state, now, minutes)
        if first == "status":
            return cmd_status(state, now)
        if first == "confirm":
            return cmd_confirm(state, now, argv[1] if len(argv) >= 2 else None)
        if first == "edit":
            return cmd_edit(state, now, argv[1:])
        if first == "skip":
            return cmd_skip(state, now, argv[1] if len(argv) >= 2 else None)
        if first == "dismiss":
            return cmd_dismiss(state, now, argv[1] if len(argv) >= 2 else None)
        if first == "draft-reply" or first == "reply":
            return cmd_draft_reply(state, now, argv[1:])
        if first == "reply-task":
            # reply-task [thread_id] [YYYY-MM-DD] [지시…]
            sub_argv: list[str] = []
            user_due: str | None = None
            for a in argv[1:]:
                if user_due is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
                    user_due = a
                else:
                    sub_argv.append(a)
            return cmd_draft_reply(state, now, sub_argv,
                                   create_gtask_too=True, user_due=user_due)
        if first == "gtask":
            return cmd_gtask(state, now, argv[1:])
        if first == "schedule":
            return cmd_schedule(state, now, argv[1:])
        if first == "nl":
            return cmd_nl(state, now, argv[1:])
        if first == "migrate-inbox":
            apply = "--apply" in argv[1:]
            return cmd_migrate_inbox(state, now, apply=apply)
        if first == "migrate-brainify-labels":
            apply = "--apply" in argv[1:]
            return cmd_migrate_brainify_labels(state, now, apply=apply)
        if first == "pending-review":
            bs = GMAIL_MAX
            if len(argv) >= 2:
                try:
                    bs = max(1, int(argv[1]))
                except ValueError:
                    pass
            return cmd_pending_review(state, now, batch_size=bs)
        if first == "bulk-reclassify":
            return cmd_bulk_reclassify(state, now, argv[1:])
        if first == "save-drain":
            return cmd_save_drain(state, now, argv[1:])
        if first == "schedule-drain":
            return cmd_schedule_drain(state, now, argv[1:])
        if first == "--force-poll" or first == "--force-batch":
            return cmd_poll(state, now, force=True)
        # unknown — fall through to poll
        print(f"[gws-assistant] 알 수 없는 인자 무시: {argv}", file=sys.stderr)

    return cmd_poll(state, now, force=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
