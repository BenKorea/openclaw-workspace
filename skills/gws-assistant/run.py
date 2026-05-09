#!/usr/bin/env python3
"""gws-assistant — Gmail 브리핑 + 브레인화 plan + Telegram 양방향 승인.

운영 모델 (Phase 1, 3분류 확정 2026-05-08):
    - cron 이 평일 10분 간격으로 본 스크립트를 폴링
    - 게이트(평일/근무시간/공휴일/미팅 중-`판독`예외) 통과 + plan 변경 시 발화
    - 발화 = unread 전부의 3분류(진행·보류·불필요) plan + Telegram 메시지 + 승인 명령 안내
    - 사용자는 Telegram 에서 /gws-assistant approve|cancel|snooze N 으로 응답
    - approve: pending plan 의 자동 처리 항목 실행 (모든 항목 라벨/archive)
    - cancel: pending plan 폐기 → 다음 폴링에서 재분류
    - snooze N: N 분 동안 발화 보류
    - pending plan 은 명시 폐기/승인 까지 살아있음 (자동 만료 없음)
    - 미응답 plan + 신규 메일은 다음 폴링에 머지되어 한 메시지로 발화

라벨 정책:
    라벨은 "콘텐츠 카테고리" 가 아니라 "브레인화 작업 진행 상태" 표시.
    - 브레인화/진행 — audit 가치 있어 브레인화로 진행할 메일 (라벨 + archive)
    - 브레인화/보류 — 외부 액션 대기 또는 분류 자신없음 (라벨, inbox 유지)
    - 브레인화/불필요 — 광고·자동알림·중복 등 브레인화 대상 아님 (라벨 + archive)
    Legacy `브레인화/완료` / `브레인화/중복` 은 그대로 두고 unread 검색에서만 제외.

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
GMAIL_MAX = 10
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
LABEL_PROCEED = "브레인화/진행"

# Legacy 라벨 — 이미 부착된 메일은 그대로 두고 unread 검색에서만 제외 (마이그레이션 안 함).
LEGACY_LABEL_COMPLETE = "브레인화/완료"
LEGACY_LABEL_DUPLICATE = "브레인화/중복"

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
    }


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open() as f:
                state = json.load(f)
            state.setdefault("last_checked", "")
            state.setdefault("snooze_until", None)
            state.setdefault("pending_plan", None)
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
    if r.returncode != 0:
        return (False, (r.stderr or r.stdout or "").strip())
    return (True, (r.stdout or "").strip())


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

def fetch_unread_all():
    """unread + brainify 라벨(3-라벨 + legacy 2개) 어디에도 안 붙은 것만. archive 된 메일 제외 (in:inbox)."""
    query = ("is:unread in:inbox "
             f"-label:{LABEL_NOISE} -label:{LABEL_PENDING} -label:{LABEL_PROCEED} "
             f"-label:{LEGACY_LABEL_COMPLETE} -label:{LEGACY_LABEL_DUPLICATE}")
    items = gog_json("gmail", "search", query, "--max", str(GMAIL_MAX))
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

def classify_email_heuristic(m: dict) -> tuple[str, str]:
    """from 주소 substring 매칭으로 noise 만 자동 식별. 그 외 pending.
    빠른 path — LLM 호출 회피용."""
    frm_lower = (m.get("from") or "").lower()
    for pattern in NOISE_FROM_PATTERNS:
        if pattern in frm_lower:
            return ("noise", pattern)
    return ("pending", "")


def classify_emails_llm(emails: list[dict]) -> dict[str, tuple[str, str]]:
    """LLM (claude haiku) 으로 batch 분류. 반환: {msg_id: (category, reason)}.
    실패 시 빈 dict (호출자가 pending fallback)."""
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

    # SSOT: vault gmail-capture.md §1·§2. 추출 실패 시에만 인라인 fallback.
    vault_guide = _vault_classification_guide()
    if vault_guide:
        guide_block = (
            "분류 기준 — 아래는 2nd-brain-vault `gmail-capture.md` §1·§2 의 권위 정의다. "
            "이 정의를 따르라.\n\n"
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

    prompt = (
        f"다음 {len(emails)}개 Gmail 메일을 분류하라. **반드시 JSON 배열만 응답하라** (다른 텍스트 금지).\n\n"
        f"{guide_block}\n"
        "출력 카테고리는 다음 3개 중 하나:\n"
        "- proceed → 라벨 '브레인화/진행' + archive\n"
        "- pending → 라벨 '브레인화/보류' + inbox 유지\n"
        "- noise   → 라벨 '브레인화/불필요' + archive (명백한 중복은 trash)\n\n"
        "응답 형식 (JSON 배열만):\n"
        '[{"id":"<msg_id>","category":"proceed|pending|noise","reason":"한 줄 이유"}]\n\n'
        "메일 목록:\n" + "\n\n".join(items_text)
    )

    # prompt 는 stdin 으로 전달. --disallowedTools 가 variadic 옵션이라
    # 그 뒤에 positional prompt 를 두면 옵션이 prompt 를 흡수해 "Input must be provided"
    # 에러가 난다 (commander.js variadic semantics).
    cmd = [
        "claude", "--print", "--permission-mode", "bypassPermissions",
        "--model", "claude-haiku-4-5",
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
            if mid and cat in valid_cats:
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
        if cat == "noise":
            items.append(_make_item(m, "noise", f"발신자 패턴 '{reason}'"))
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
    """CATEGORY_ORDER 우선순위 + 같은 분류 안에선 date 역순 (최신이 위).
    메시지 가독성 + Ben 이 자연스럽게 위에서 아래로 읽도록."""
    def key(it):
        cat = it.get("category", "pending")
        try:
            cat_idx = CATEGORY_ORDER.index(cat)
        except ValueError:
            cat_idx = len(CATEGORY_ORDER)
        # date 는 문자열 — 역순 정렬을 위해 음수 ordering 트릭 대신 reverse 적용 어려우니
        # tuple 의 두 번째 요소는 negated date 처럼 역순 비교 가능한 값을 만들어야.
        # 단순화: 같은 카테고리 안에선 안정 정렬 + 외부에서 fetch 순서가 이미 최신순이므로 그대로.
        return (cat_idx,)
    return sorted(items, key=key)


def merge_plan(state: dict, emails: list[dict], now: dt.datetime) -> dict:
    """현재 unread emails 를 분류(캐시 활용) 후 pending plan 에 머지.
    - 신규 메일만 LLM 호출. 기존 plan 의 msg_id 는 분류 그대로 재사용.
    - 외부에서 처리된 (unread 에서 사라진) msg_id 는 plan 에서 제거.
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
    return f"{num}. {date}  {frm}\n   제목: {subj}"


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
        L.append(f"안 읽은 메일 {total_count}건이 있습니다. 아래 3분류대로 브레인화를 진행할지 검토 바랍니다.")
    else:
        L.append(f"안 읽은 메일 {total_count}건이 있습니다 (이번에 신규 {new_count}건 추가). 아래 3분류대로 브레인화를 진행할지 검토 바랍니다.")
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

    proceed_header = (
        f"▸ 진행 {n_proceed}건 — 거래·증빙성 메일 (audit trail 가치). approve 시 본문 fetch → "
        f"동반 노트 작성(PARA 위치·연결 후보 LLM 추론 포함) → **1건씩 보고드린 뒤 승인 받음**."
    )
    pending_header = (
        f"▸ 보류 {n_pend}건 — 외부 다운로드·후속 행동 필요 또는 분류 자신없음. approve 시 라벨 "
        f"'{LABEL_PENDING}' 만 부착하고 inbox 유지. Ben 외부 작업 후 별도 brainify."
    )
    noise_header = (
        f"▸ 불필요 {n_noise}건 — 광고·뉴스레터·자동 알림·중복. approve 시 라벨 "
        f"'{LABEL_NOISE}' + archive."
    )

    emit_section(proceed_header, by_cat.get("proceed", []))
    emit_section(pending_header, by_cat.get("pending", []))
    emit_section(noise_header, by_cat.get("noise", []))

    L.append("[명령]")
    L.append("  /gws-assistant approve   — 보류·불필요 일괄 처리 + 진행 1건씩 검토 시작")
    L.append("  /gws-assistant cancel    — plan 폐기, 다음 폴링에서 재분류")
    L.append("  /gws-assistant snooze 60 — 60분 발화 보류")
    L.append("")
    L.append("(진행 항목은 approve 후 1건씩 보고됩니다 — 각 보고에 confirm/edit/skip 명령이 함께 옵니다.)")
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


def finalize_proceed(item: dict) -> tuple[bool, str]:
    """Brainify Phase 2 — 사용자 confirm 후:
    1) PARA 폴더로 노트·첨부 이동 (proposed_para_path 가 있으면)
    2) 라벨 + archive
    노트 자체는 propose 단계에서 이미 작성됨."""
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
        "--add", LABEL_PROCEED, "--remove", "INBOX",
    )
    if not ok:
        return (False, f"라벨/archive 실패: {err}")
    item["confirm_status"] = "confirmed"
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


def _parse_edit_args(argv: list[str]) -> tuple[str, str | None, list[str] | None]:
    """edit <thread_id> [folder=…] [links=<[[A]],[[B]]>] 파싱."""
    if not argv:
        return ("", None, None)
    tid = argv[0]
    folder: str | None = None
    links: list[str] | None = None
    # links= 값에 쉼표가 있을 수 있으니, argv 결합 후 key= 분리.
    rest = " ".join(argv[1:])
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
    L.append("")
    tid = item.get("msg_id", "")
    L.append("[명령]")
    L.append(f"  /gws-assistant confirm {tid}")
    L.append(f"      → 그대로 확정 (라벨 '{LABEL_PROCEED}' + archive)")
    L.append(f"  /gws-assistant edit {tid} folder=<경로> links=<[[A]],[[B]]>")
    L.append(f"      → PARA 위치/연결 수정 후 확정")
    L.append(f"  /gws-assistant skip {tid}")
    L.append(f"      → 이 메일 건너뛰기 (라벨 '{LABEL_PENDING}', inbox 유지, 노트 정리)")
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


def cmd_confirm(state: dict, now: dt.datetime, thread_id: str) -> int:
    plan = state.get("pending_plan")
    if not plan:
        print("[브레인화] pending plan 이 없습니다.")
        return 0
    item = _find_item_by_thread(plan, thread_id)
    if not item:
        print(f"[브레인화] thread_id '{thread_id}' 항목을 찾을 수 없습니다.")
        return 0
    if item.get("category") != "proceed":
        print(f"[브레인화] proceed 항목이 아닙니다 (category={item.get('category')}).")
        return 0
    if item.get("confirm_status") != "pending_review":
        print(f"[브레인화] 이미 처리된 항목입니다 (status={item.get('confirm_status')}).")
        return 0

    ok, err = finalize_proceed(item)
    if not ok:
        print(f"[브레인화 확정 실패] {err}\n해당 항목은 큐에 남아있습니다.")
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    para = item.get("proposed_para_path") or "sources/00_inbox/"
    header = (
        f"[브레인화 확정] {_short_subject(item.get('subject',''), 40)} "
        f"→ 노트 위치 {para}, 라벨 '{LABEL_PROCEED}' + archive\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def cmd_edit(state: dict, now: dt.datetime, argv: list[str]) -> int:
    thread_id, folder, links = _parse_edit_args(argv)
    if not thread_id:
        print("[브레인화] edit 명령에는 thread_id 가 필요합니다.")
        return 1
    plan = state.get("pending_plan")
    if not plan:
        print("[브레인화] pending plan 이 없습니다.")
        return 0
    item = _find_item_by_thread(plan, thread_id)
    if (not item or item.get("category") != "proceed"
            or item.get("confirm_status") != "pending_review"):
        print(f"[브레인화] thread_id '{thread_id}' 가 review 대기 상태가 아닙니다.")
        return 0
    if folder is None and links is None:
        print("[브레인화] edit 에 folder= 또는 links= 중 하나는 필요합니다.")
        return 1

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
    header = (
        f"[브레인화 수정 후 확정] {_short_subject(item.get('subject',''), 40)} "
        f"→ 노트 위치 {para}, 라벨 '{LABEL_PROCEED}' + archive\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


def cmd_skip(state: dict, now: dt.datetime, thread_id: str) -> int:
    plan = state.get("pending_plan")
    if not plan:
        print("[브레인화] pending plan 이 없습니다.")
        return 0
    item = _find_item_by_thread(plan, thread_id)
    if (not item or item.get("category") != "proceed"
            or item.get("confirm_status") != "pending_review"):
        print(f"[브레인화] thread_id '{thread_id}' 가 review 대기 상태가 아닙니다.")
        return 0

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

    header = (
        f"[브레인화 건너뛰기] {_short_subject(item.get('subject',''), 40)} "
        f"→ 라벨 '{LABEL_PENDING}' (inbox 유지). 노트 파일 정리됨.\n\n"
    )
    msg = _propose_next_or_complete(state, plan, now, header)
    print(msg)
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


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
    print(f"[gws-assistant status] now={now.isoformat()}", file=sys.stderr)
    print(f"  last_checked: {state.get('last_checked', '')}", file=sys.stderr)
    print(f"  snooze_until: {su or '(없음)'}", file=sys.stderr)
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

def cmd_poll(state: dict, now: dt.datetime, force: bool) -> int:
    """1. Gates → 통과해야 발화.
       2. Snooze → 활성 시 침묵.
       3. fetch unread → classify → merge into pending plan.
       4. plan 의 msg_id set 가 마지막 발화 set 와 다르면 발화."""
    if not force:
        gate_fail = check_gates(now)
        if gate_fail:
            state["last_checked"] = now.isoformat()
            save_state(state)
            return 0

        if is_snoozed(state, now):
            state["last_checked"] = now.isoformat()
            save_state(state)
            return 0

    emails = fetch_unread_all()
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
        # 변경 사항만 plan 에 반영해두고 침묵
        if removed:
            # 외부에서 처리된 항목 반영 — last_announced 도 정리
            plan["last_announced_msg_ids"] = sorted(current_ids)
        state["pending_plan"] = plan if current_ids else None
        state["last_checked"] = now.isoformat()
        save_state(state)
        return 0

    msg = format_plan_message(now, plan, new_count=len(new_in_plan), total_count=len(current_ids))
    print(msg)

    plan["last_announced_msg_ids"] = sorted(current_ids)
    state["pending_plan"] = plan if current_ids else None
    state["last_checked"] = now.isoformat()
    save_state(state)
    return 0


# ============================================================================
# Main / arg parsing
# ============================================================================

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
            if len(argv) < 2:
                print("[브레인화] confirm 명령에는 thread_id 가 필요합니다.")
                return 1
            return cmd_confirm(state, now, argv[1])
        if first == "edit":
            return cmd_edit(state, now, argv[1:])
        if first == "skip":
            if len(argv) < 2:
                print("[브레인화] skip 명령에는 thread_id 가 필요합니다.")
                return 1
            return cmd_skip(state, now, argv[1])
        if first == "migrate-inbox":
            apply = "--apply" in argv[1:]
            return cmd_migrate_inbox(state, now, apply=apply)
        if first == "--force-poll" or first == "--force-batch":
            return cmd_poll(state, now, force=True)
        # unknown — fall through to poll
        print(f"[gws-assistant] 알 수 없는 인자 무시: {argv}", file=sys.stderr)

    return cmd_poll(state, now, force=False)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
