"""webmail-watch — 외부 forwarding/IMAP 차단된 webmail polling → webmail UI "전달" click-driven re-forward.

사용법:
    python -m skills.webmail-watch.run <tenant>             # cron headless poll
    python -m skills.webmail-watch.run <tenant> --bootstrap # 1회 수동 로그인 (headed)

자격증명·OTP 어느 것도 stdout/log 로 흐르지 않음.
외부 SMTP/API 발송 채널 ✗ — KIRAMS UI 의 "전달" 버튼을 Playwright 가 클릭.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pyotp
from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

KST = timezone(timedelta(hours=9))

HOME = Path.home()
SECRETS_DIR = HOME / ".openclaw" / "secrets"
PROFILE_ROOT = HOME / ".openclaw" / "skills" / "webmail-watch" / "chrome-profile"
STATE_PATH = HOME / ".openclaw" / "agents" / "main" / "memory" / "webmail-watch.json"

FORWARD_TO = "kimbi.kirams@gmail.com"
SUBJECT_PREFIX = "[KIRAMS-FWD] "

log = logging.getLogger("webmail-watch")


@dataclass(frozen=True)
class TenantConfig:
    key: str
    label: str
    entry_url: str
    inbox_url: str
    detail_url_re: re.Pattern[str]
    totp_secret_path: Path
    selectors: dict[str, str] = field(default_factory=dict)
    poll_limit: int = 3


TENANTS: dict[str, TenantConfig] = {
    "kirams": TenantConfig(
        key="kirams",
        label="KIRAMS",
        entry_url="https://mail.kirams.re.kr/member/login",
        inbox_url="https://mail.kirams.re.kr/mail/inbox",
        detail_url_re=re.compile(r"/mail/inbox/messages/(\d+)"),
        totp_secret_path=SECRETS_DIR / "webmail-watch-kirams.toml",
        selectors={
            # login (P3.1·P3.2 확정). ID 는 KIRAMS "아이디저장" prefill.
            "login_pw": "#cpw",
            "login_submit": "#btnlogin",
            "otp_input": "#otp_code1",
            "otp_submit": "",  # 별도 버튼 ✗ — page.press(otp_input, "Enter") 로 submit
            "inbox_marker": "tbody tr[data-index]",
            "login_form_marker": "#cpw",
            # listing. MailPlug 새 빌드(2026-05 개편): 행을 열지 않음 — 체크박스 선택 →
            # 툴바 "전달" 직접. detail URL/메일 열기 단계 ✗. 발신자는 행 내 유일한
            # span[title] ("이름 <addr>"); 버튼들도 [title] 라 span 한정 필수.
            "row_from_title": "span[title]",
            "row_subject_text": "span.break-all",
            # forward UI. 체크박스 선택 후에만 enabled. class 가 build hash 라 텍스트 기반.
            "forward_button": 'button:has-text("전달")',
            "forward_to_input": "#toRecipients-input",
            "forward_subject_input": "#input-subject",
            "forward_send_button": 'button:has-text("보내기")',
            # move UI (받은편지함 toolbar — forward 후 Gmail 폴더로 이동).
            # 실제 input 은 sr-only 처리됨 → 시각 element 인 <label for="toggle-0"> 에 click.
            "row_checkbox": 'label[for="toggle-0"]',
            "toolbar_move_button": 'button:has-text("이동")',
            "move_dropdown": '[aria-labelledby="dropdown-toggle-move"][role="menu"]',
            "move_target_gmail": '[aria-labelledby="dropdown-toggle-move"] a:has-text("Gmail")',
        },
    ),
}


@dataclass
class Outcome:
    forwarded: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    bounce_halt: bool = False
    bounce_subjects: list[str] = field(default_factory=list)


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("webmail-watch.json corrupt — resetting to empty")
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".webmail-watch.json.", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def load_secret(tenant: TenantConfig) -> dict[str, Any]:
    """tenant secret toml 의 해당 섹션만 read.

    파일 구조: `[<TENANT>]` 섹션(대소문자 무관) 안의 키들. 평탄 구조도 fallback 지원.
    호출자는 사용 직후 dict.clear() 로 GC 유도. 절대 log/stdout 으로 흘리지 ✗.
    """
    raw = load_toml(tenant.totp_secret_path)
    for key, value in raw.items():
        if key.lower() == tenant.key.lower() and isinstance(value, dict):
            return value
    return raw


def totp_code_from_secret(secret: dict[str, Any]) -> str:
    return pyotp.TOTP(
        secret["otp_secret"],
        digits=int(secret.get("otp_digits", 6)),
        interval=int(secret.get("otp_period", 30)),
    ).now()


def totp_code(tenant: TenantConfig) -> str:
    return totp_code_from_secret(load_secret(tenant))


def open_context(pw: Playwright, tenant: TenantConfig, headless: bool) -> BrowserContext:
    profile_dir = PROFILE_ROOT / tenant.key
    profile_dir.mkdir(parents=True, exist_ok=True)
    _kwargs = dict(
        user_data_dir=str(profile_dir),
        # [사이드카] full chromium = MailPlug Next.js SPA 렌더용 (env WEBMAIL_CHANNEL override).
        channel=(os.environ.get("WEBMAIL_CHANNEL", "chromium") or None),
        headless=headless,
        # KIRAMS reactive UI timing race 방어막. 기본 1500ms — 800ms 는 배치에서 첫 건 이동 후
        # SPA 미정착 상태로 다음 행 잡다 정지하는 race 발생(2026-06-23 실측). env WEBMAIL_SLOW_MO override.
        slow_mo=int(os.environ.get("WEBMAIL_SLOW_MO", "1500")),
        accept_downloads=True,
        viewport={"width": 1280, "height": 900},
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    # 데모 녹화 — WEBMAIL_RECORD_DIR 설정 시 세션 전체를 webm 영상으로 저장(context close 시 확정).
    _rec = os.environ.get("WEBMAIL_RECORD_DIR", "").strip()
    if _rec:
        os.makedirs(_rec, exist_ok=True)
        _kwargs["record_video_dir"] = _rec
        _kwargs["record_video_size"] = {"width": 1280, "height": 900}
    return pw.chromium.launch_persistent_context(**_kwargs)


def is_logged_in(page: Page, tenant: TenantConfig) -> bool:
    sel = tenant.selectors
    try:
        page.wait_for_selector(sel["inbox_marker"], timeout=5_000)
        return True
    except PlaywrightTimeoutError:
        # [사이드카] 빈 받은편지함: 메일 행(marker) 0개라 위 timeout → 로그인은 됐는데
        # 메일 0건인데도 not-logged-in 으로 오판(false auth_failed → 운영서 에러 알림 노이즈).
        # 로그인 시 항상 있는 안정 요소("메일이 없습니다" 빈표시 / "메일 쓰기" 작성버튼)로 보강.
        try:
            for _t in ("메일이 없습니다", "메일 쓰기"):
                if page.get_by_text(_t, exact=False).count() > 0:
                    return True
        except Exception:
            pass
        return False


def perform_login(page: Page, tenant: TenantConfig) -> bool:
    """자동 재로그인 — toml 기반 PW 직접 fill + TOTP 자동 입력.

    ID 는 KIRAMS "아이디저장" 기능으로 prefill 되므로 PW 만 입력 → 로그인 버튼 → OTP 화면.
    """
    sel = tenant.selectors

    try:
        page.wait_for_selector(sel["login_form_marker"], timeout=10_000)
    except PlaywrightTimeoutError:
        return False

    secret = load_secret(tenant)
    try:
        # [컨테이너] 프로필 prefill 대신 ID 명시 입력 — fresh/headless 프로필엔 "아이디저장" 부재.
        # ID = otp_account(TOTP 라벨)의 @ 앞부분. 새 자격증명 불요.
        _login_id = (secret.get("otp_account") or "").split("@")[0].strip()
        if _login_id:
            try:
                # ★ fill() 은 값만 설정 → MailPlug React 가 로그인 버튼 활성화에 쓰는 키입력
                # 이벤트가 안 떠 #btnlogin 이 disabled 유지(2026-05-25 실측, 수동 타이핑은 정상).
                # 한 글자씩 타이핑(press_sequentially)으로 교체해 실제 키 이벤트 발생시킴.
                page.locator("#cid").press_sequentially(_login_id, delay=50, timeout=5_000)
            except PlaywrightTimeoutError as e:
                log.error("login_id(#cid) 입력 실패: %s", type(e).__name__)
        else:
            log.error("login_id 도출 실패 (otp_account 없음)")
        try:
            page.locator(sel["login_pw"]).press_sequentially(secret["login_pw"], delay=50, timeout=5_000)
        except (PlaywrightTimeoutError, KeyError) as e:
            log.error("login_pw 입력 실패: %s", type(e).__name__)
            return False

        page.click(sel["login_submit"])

        try:
            page.wait_for_selector(sel["otp_input"], timeout=10_000)
            code = totp_code_from_secret(secret)
            page.locator(sel["otp_input"]).press_sequentially(code, delay=50)
            del code
            if sel.get("otp_submit"):
                page.click(sel["otp_submit"])
            else:
                # KIRAMS: 별도 submit 버튼 ✗ — Enter 로 폼 submit. fill() 은 input 이벤트만 발생,
                # keydown/keypress 가 필요한 사이트는 page.press 로 보강.
                page.press(sel["otp_input"], "Enter")
            # MailPlug 새 빌드: OTP 후 /mail 로 redirect 하지만 inbox 테이블이
            # 자동 렌더링 ✗ → inbox_url 명시 이동 후 marker 확인.
            try:
                # 낙관적 대기 — redirect 가 패턴과 맞으면 즉시 통과, 아니면 짧게만 기다리고
                # 아래 goto(inbox_url) + inbox_marker 대기가 실제 안전망. (15s→4s: OTP 후 dead-wait 단축)
                page.wait_for_url("**/mail**", timeout=4_000)
            except PlaywrightTimeoutError:
                pass
            page.goto(tenant.inbox_url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_selector(sel["inbox_marker"], timeout=15_000)
                return True
            except PlaywrightTimeoutError:
                return False
        except PlaywrightTimeoutError:
            pass

        return is_logged_in(page, tenant)
    finally:
        secret.clear()


def _ensure_inbox(page: Page, tenant: TenantConfig) -> None:
    try:
        page.wait_for_selector(tenant.selectors["inbox_marker"], timeout=2_000)
    except PlaywrightTimeoutError:
        page.goto(tenant.inbox_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector(tenant.selectors["inbox_marker"], timeout=15_000)
    _wait_for_ui_idle(page)


def _wait_for_ui_idle(page: Page, timeout: int = 2_000) -> None:
    """KIRAMS SPA 의 로딩 오버레이 / headlessui portal toast 가 정착할 때까지 wait.

    timeout 시 silent fallthrough — 후속 click 의 자체 retry/intercept 처리에 맡김.
    검사가 영구 false 패턴이어도 짧은 fixed wait 효과로 동작 (실측 검증).
    """
    try:
        page.wait_for_function(
            """() => {
                const overlay = document.querySelector(
                    'div[class*="!absolute"][class*="inset-0"][class*="bg-white"]'
                );
                const portalChildren = document.querySelectorAll('#headlessui-portal-root > *').length;
                return !overlay && portalChildren === 0;
            }""",
            timeout=timeout,
        )
    except PlaywrightTimeoutError:
        log.debug("UI idle wait timeout — continuing")


def _wait_toolbar_enabled(page: Page, label: str, timeout: int = 8_000) -> None:
    """텍스트가 정확히 `label` 인 toolbar 버튼이 enabled 될 때까지 wait.

    MailPlug 새 빌드: 행 체크박스 미선택 시 전달/이동 등 toolbar 버튼이 disabled.
    """
    page.wait_for_function(
        """(lbl) => {
            const b = [...document.querySelectorAll('button')]
                .find(x => (x.innerText || '').trim() === lbl);
            return !!b && !b.disabled;
        }""",
        arg=label,
        timeout=timeout,
    )


class OversizeForwardError(Exception):
    """forward 대상 메일이 MailPlug 20MB 한도 초과 — compose 가 안 열리고 차단 모달이 뜸."""


def forward_via_webmail(page: Page, tenant: TenantConfig, row_idx: int = 0) -> None:
    """받은편지함 listing 상태에서 호출 (MailPlug 새 빌드 모델).

    행 체크박스 선택 → toolbar "전달" → /mail/write compose → 발송 → 받은편지함 복귀.
    메일을 따로 열지 않음 (구 빌드의 detail page 단계 제거).
    실패 시 예외 raise → caller 가 partial_failure 처리.
    """
    sel = tenant.selectors

    _wait_for_ui_idle(page)

    # 첫 row 체크박스 선택 → toolbar 활성화
    page.locator(f'label[for="toggle-{row_idx}"]').first.click(timeout=10_000)
    _wait_toolbar_enabled(page, "전달")

    page.locator(sel["forward_button"]).first.click(timeout=10_000)
    # "전달" 후 둘 중 하나: compose 수신자칸(정상) 또는 "최대 20MB" 차단 모달(용량초과).
    # 1초 간격 폴로 먼저 나타나는 쪽 감지 — 정상·초과 둘 다 빠르게 판정(15초 낭비 회피).
    oversize = False
    for _ in range(16):
        if page.locator(sel["forward_to_input"]).count() > 0:
            break
        if page.locator('text=최대 20MB').count() > 0:
            oversize = True
            break
        page.wait_for_timeout(1000)
    if oversize:
        # 모달 확인 버튼은 정확 텍스트 "확인" — has-text 면 툴바 "수신 확인" 을 오매치하므로 role+exact.
        page.get_by_role("button", name="확인", exact=True).first.click(timeout=5_000)
        page.wait_for_selector('text=최대 20MB', state="detached", timeout=5_000)
        # 선택 해제 — 다음 행 처리 시 다중선택 방지(이 행 체크박스는 아직 켜진 상태).
        try:
            page.locator(f'label[for="toggle-{row_idx}"]').first.click(timeout=5_000)
        except Exception:
            pass
        raise OversizeForwardError()
    page.wait_for_selector(sel["forward_to_input"], timeout=15_000)

    # 제목은 새 빌드에서 비동기로 `[FW]<원제목>` 자동 채움 (compose 진입 직후 ~1s 공백).
    # 공백 상태에서 prefix 만 넣으면 원제목 유실 → gmail-label-actions 분류 불가.
    # 자동 채움 완료(non-empty)까지 wait 후 prefix prepend.
    subject_input = page.locator(sel["forward_subject_input"])
    try:
        page.wait_for_function(
            """(sel) => {
                const e = document.querySelector(sel);
                return !!e && e.value.trim().length > 0;
            }""",
            arg=sel["forward_subject_input"],
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        log.debug("subject auto-fill wait timeout — prefix only")
    current = subject_input.input_value() or ""
    if not current.startswith(SUBJECT_PREFIX):
        subject_input.fill(SUBJECT_PREFIX + current)

    # 받는사람 chip 입력. Enter 는 chip 변환 + form submit 둘 다 trigger 하여
    # 명시적 보내기 click 과 합쳐 발송 2회 발생 → Tab 으로 chip 변환만.
    to_input = page.locator(sel["forward_to_input"])
    to_input.click()
    to_input.fill(FORWARD_TO)
    to_input.press("Tab")

    page.locator(sel["forward_send_button"]).first.click(timeout=10_000)

    try:
        page.wait_for_selector(sel["inbox_marker"], timeout=30_000)
    except PlaywrightTimeoutError:
        page.goto(tenant.inbox_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_selector(sel["inbox_marker"], timeout=15_000)


def move_to_gmail_folder(page: Page, tenant: TenantConfig, row_idx: int = 0) -> None:
    """받은편지함 listing 의 첫 row 를 Gmail 폴더로 이동.

    forward_via_webmail 호출 후 받은편지함 복귀 상태에서 caller 가 호출.
    실패 시 예외 raise → 다음 회차 dup forward 위험 (옵션 1: dup 허용).
    """
    sel = tenant.selectors

    # 첫 row 체크박스 click → 선택 → toolbar 활성화
    _wait_for_ui_idle(page)
    page.locator(f'label[for="toggle-{row_idx}"]').first.click(timeout=10_000)
    _wait_toolbar_enabled(page, "이동")

    page.locator(sel["toolbar_move_button"]).first.click(timeout=10_000)
    page.wait_for_selector(sel["move_dropdown"], timeout=10_000)

    page.locator(sel["move_target_gmail"]).first.click(timeout=10_000)

    # listing 갱신 — 그 row 가 사라짐. dropdown 닫힘 + 첫 row 의 mid 변동.
    # 결정적 marker 가 없어 짧은 정착 대기 + listing re-stabilize.
    page.wait_for_selector(sel["move_dropdown"], state="detached", timeout=10_000)
    page.wait_for_selector(sel["inbox_marker"], timeout=15_000)


_BOUNCE_SUBJECT = re.compile(
    r"failure notice|mail delivery (failed|subsystem)|undelivered mail|delivery status notification"
    r"|returned mail|mail delivery failure|반송|배달.*실패|전송.*실패", re.I)
_BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|qmail|mail delivery", re.I)


def _is_bounce(from_text: str, subject_text: str) -> bool:
    """반송(bounce)/failure notice 인지 — 전달 대상에서 제외. 552 반송이 만든 노이즈·악순환 차단.

    KIRAMS qmail 은 발송 실패 시 'failure notice'(From: MAILER-DAEMON 류) 를 받은편지함에 남기는데,
    이를 그대로 forward 하면 Gmail 에 반송 노이즈만 쌓이고 큐가 안 빠진다 → 전달 skip.
    """
    return bool(_BOUNCE_SUBJECT.search(subject_text or "") or _BOUNCE_FROM.search(from_text or ""))


def process_inbox(page: Page, tenant: TenantConfig,
                  limit: "int | None" = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """받은편지함 = pending queue. 위에서부터 limit(미지정 시 tenant.poll_limit) 건 처리.

    각 회: 첫 row 메타 추출 → forward(체크박스+전달) → 받은편지함 복귀 → Gmail 이동.
    listing 빔 또는 forward/move 실패 시 break.
    limit: 수동 on-demand 호출(`--limit N`)이 이번 실행만 건수 오버라이드.

    MailPlug 새 빌드(2026-05): 메일을 여는 detail page/URL 단계 ✗ —
    체크박스 선택 후 toolbar "전달" 로 직접 compose 진입. message_id 추적 무의미.
    """
    sel = tenant.selectors
    forwarded: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    bounce_subjects: list[str] = []   # 받은편지함서 만난 failure notice(과거 반송 증거) — 발견 시 작동 중단 신호

    _n = limit if limit is not None else tenant.poll_limit
    skip = 0          # 선두에 쌓인 forward-불가(용량초과 등) 메일 수 — 그만큼 아래 행을 처리
    processed = 0
    guard = 0         # 무한루프 방지 (오버사이즈 다수 대비 여유)
    while processed < _n and guard < _n + 30:
        guard += 1
        _ensure_inbox(page, tenant)

        rows = page.locator(f'tbody tr[data-index="{skip}"]')
        if rows.count() == 0:
            break
        row = rows.first

        try:
            from_text = (row.locator(sel["row_from_title"]).first.get_attribute("title") or "").strip()
        except PlaywrightTimeoutError:
            from_text = ""
        try:
            subject_text = row.locator(sel["row_subject_text"]).first.inner_text(timeout=5_000).strip()
        except PlaywrightTimeoutError:
            subject_text = ""

        # 새 빌드는 메일 열기/ detail URL 단계 ✗ → message_id 추적 무의미.
        # forwarded 메타용 식별자는 제목으로 충분 (Telegram 침묵 정책).
        mid = "?"

        # 반송(failure notice) = 과거 전달이 552 등으로 실패했다는 증거(비동기 도착).
        # 전달하지 않고 받은편지함에 남긴 채 기록 → 이번 run 의 깨끗한 메일은 계속 처리하되,
        # run 종료 시 report() 가 텔레그램 알림 + 작동 중단(타이머 disable) 신호를 낸다 (Dr. Ben 수동 처리).
        if _is_bounce(from_text, subject_text):
            log.info("bounce/failure notice 감지 — 전달 skip, 작동 중단 예정: %s", subject_text or "?")
            bounce_subjects.append(subject_text or "failure notice")
            skip += 1
            continue

        try:
            forward_via_webmail(page, tenant, row_idx=skip)
        except OversizeForwardError:
            # MailPlug 20MB 한도 초과 — 자동 전달 불가. 받은편지함에 남겨(가시) 다음 행으로.
            log.warning("oversize >20MB — skip & leave in inbox: %s", subject_text or "?")
            failures.append({"id": mid, "subject": subject_text, "error": "oversize>20MB"})
            skip += 1
            continue
        except Exception as e:
            log.exception("forward 실패: mid=%s", mid)
            failures.append({"id": mid, "subject": subject_text, "error": f"forward:{type(e).__name__}"})
            break

        try:
            move_to_gmail_folder(page, tenant, row_idx=skip)
        except Exception as e:
            log.exception("move 실패: mid=%s", mid)
            failures.append({"id": mid, "subject": subject_text, "error": f"move:{type(e).__name__}"})
            break

        forwarded.append({"id": mid, "from": from_text, "subject": subject_text})
        processed += 1

    return forwarded, failures, bounce_subjects


def notify_telegram(text: str, push: bool = False) -> None:
    """알림. **기본은 stderr 로그만**(성공·일반 알림은 Telegram 침묵 — 매 회차 spam 방지,
    gmail-label-actions 의 Gmail 브리핑에 흡수). **push=True 일 때만** Telegram Bot API 직접 발송 —
    반송(failure notice) 감지처럼 Dr. Ben 의 *즉시 수동 개입* 이 필요한 알림 전용.

    봇토큰은 사이드카에 파일로 마운트하지 않는다(최소권한 유지). 호스트 래퍼(poll.sh)가
    openclaw.json 에서 읽어 *이 run 에 한해* env 로 주입. 미주입(수동 디버그 등) 시 조용히 로그 fallback.
    """
    log.info("notify: %s", text)
    if not push:
        return
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not tok or not chat:
        return
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data),
            timeout=15)
    except Exception as e:
        log.warning("telegram 발송 실패: %s", type(e).__name__)


def cron_run(tenant: TenantConfig, limit: "int | None" = None) -> Outcome:
    state = load_state()
    outcome = Outcome()

    with sync_playwright() as pw:
        # [사이드카] MailPlug SPA 는 headless 면 백지(headless 탐지/렌더 surface 요구) →
        # WEBMAIL_HEADED=1 + xvfb-run 으로 headed 구동 시 정상 렌더(demo 와 동일 경로).
        _headed = os.environ.get("WEBMAIL_HEADED", "0") == "1"
        ctx = open_context(pw, tenant, headless=not _headed)
        page = ctx.new_page()
        page.goto(tenant.entry_url, wait_until="domcontentloaded", timeout=30_000)

        # MailPlug 새 빌드: entry_url 에서 세션 유효 시 /mail 로 redirect 하지만
        # inbox 테이블이 자동 렌더링 ✗. 로그인 페이지가 아닌 경우 inbox_url 명시 이동.
        if "/member/login" not in page.url:
            try:
                page.goto(tenant.inbox_url, wait_until="domcontentloaded", timeout=30_000)
            except PlaywrightTimeoutError:
                pass

        if not is_logged_in(page, tenant):
            if not perform_login(page, tenant):
                outcome.error = "auth_failed"
                try:  # [컨테이너 진단용] 실패 시점 페이지 상태 + DOM 덤프
                    page.screenshot(path="/home/node/.openclaw/wmw-authfail.png", full_page=True)
                    _html = page.content()
                    open("/home/node/.openclaw/wmw-inbox-dom.html", "w").write(_html)
                    _body = page.inner_text("body")
                    log.info("diag: url=%s title=%r htmllen=%d bodytextlen=%d",
                             page.url, page.title(), len(_html), len(_body))
                    log.info("diag bodytext[:1500]: %r", _body[:1500])
                    for _kw in ["지원하지", "브라우저", "오류", "Error", "error", "alert",
                                "로딩", "loading", "스크립트", "script", "차단", "이동"]:
                        if _kw in _html:
                            log.info("diag kw 발견: %s", _kw)
                    log.info("diag elems: div=%d script=%d table=%d tr=%d",
                             len(page.query_selector_all("div")),
                             len(page.query_selector_all("script")),
                             len(page.query_selector_all("table")),
                             len(page.query_selector_all("tr")))
                except Exception as _e:
                    log.info("diag 실패: %s", type(_e).__name__)
                ctx.close()
                return outcome

        try:
            outcome.forwarded, outcome.failures, outcome.bounce_subjects = process_inbox(page, tenant, limit=limit)
            outcome.bounce_halt = bool(outcome.bounce_subjects)
        except Exception as e:
            log.exception("process_inbox 예외")
            outcome.error = f"process_failed: {type(e).__name__}"
            ctx.close()
            return outcome

        _video = page.video if os.environ.get("WEBMAIL_RECORD_DIR", "").strip() else None
        ctx.close()
        if _video is not None:
            try:
                log.info("📹 demo 영상 저장: %s", _video.path())
            except Exception as _e:
                log.info("영상 경로 조회 실패: %s", type(_e).__name__)

    state.setdefault(tenant.key, {})
    state[tenant.key].pop("last_message_id", None)
    state[tenant.key]["last_checked"] = datetime.now(KST).isoformat(timespec="seconds")
    save_state(state)

    return outcome


def bootstrap_run(tenant: TenantConfig) -> None:
    print(f"[bootstrap] {tenant.label} — headed Chrome 실행. WSLg 필요.")
    print(f"[bootstrap] 프로필 경로: {PROFILE_ROOT / tenant.key}")
    print(f"[bootstrap] secret 파일: {tenant.totp_secret_path}")
    print("[bootstrap] run.py 가 toml 기반 ID/PW + OTP 를 자동 주입합니다.")
    print("[bootstrap] 사람은 모니터링만 — 받은편지함 도달 후 창 닫기.")

    with sync_playwright() as pw:
        ctx = open_context(pw, tenant, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(tenant.entry_url, wait_until="domcontentloaded", timeout=30_000)

        ok = perform_login(page, tenant)
        if ok:
            print("[bootstrap] 자동 로그인 통과 — 받은편지함 도달.")
        else:
            print("[bootstrap] 자동 로그인 실패 — selector 또는 secret 점검 필요. 창 닫으시면 됩니다.")

        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        ctx.close()

    print("[bootstrap] 종료.")


def report(tenant: TenantConfig, outcome: Outcome) -> int:
    if outcome.error:
        notify_telegram(f"⚠ {tenant.label} webmail {outcome.error}")
        return 1

    if outcome.forwarded:
        notify_telegram(
            f"📨 {tenant.label} webmail 신규 {len(outcome.forwarded)}건 → forwarding 완료"
        )
    if outcome.bounce_halt:
        notify_telegram(
            f"🛑 {tenant.label} 포워딩 반송(failure notice) {len(outcome.bounce_subjects)}건 감지 — 자동 포워딩 중단.\n"
            f"Gmail 이 콘텐츠 보안(552)으로 거부한 메일입니다. KIRAMS 받은편지함의 'failure notice' 를 열어\n"
            f"어떤 메일이 반송됐는지 확인하고, 해당 원본(‘Gmail’ 보관함, 미배달)을 수동 처리하세요.\n"
            f"처리 + failure notice 삭제 후 → /cron on (또는 사이드카 타이머 재가동)으로 재개.",
            push=True,   # 반송 알림만 Telegram 푸시 (성공·실패는 log-only)
        )
        print("BOUNCE_HALT", flush=True)   # 호스트 래퍼(poll.sh)가 이 마커로 타이머 disable 판단
    if outcome.failures:
        notify_telegram(
            f"⚠ {tenant.label} {len(outcome.failures)}건 forwarding 실패 — 다음 회차 재시도"
        )
    return 0 if not (outcome.failures or outcome.bounce_halt) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="webmail-watch")
    parser.add_argument("tenant", choices=sorted(TENANTS.keys()))
    parser.add_argument("--bootstrap", action="store_true", help="1회 수동 로그인 (headed)")
    parser.add_argument("--limit", type=int, default=None,
                        help="수동 on-demand: 이번 실행만 forwarding 건수 N "
                             "(미지정=스케줄 기본 poll_limit=3). 스케줄과 무관하게 즉시 N건.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 은 1 이상의 정수")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    tenant = TENANTS[args.tenant]

    if args.bootstrap:
        bootstrap_run(tenant)
        return 0

    outcome = cron_run(tenant, limit=args.limit)
    return report(tenant, outcome)


if __name__ == "__main__":
    sys.exit(main())
