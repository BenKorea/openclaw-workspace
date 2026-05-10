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
            # listing (P5.1·P5.2 확정). message_id 는 click 후 URL 에서 추출.
            "row_open_trigger": '[role="button"][tabindex="0"]',
            "row_from_title": "[title]",
            "row_subject_text": '[role="button"] span.break-all',
            # forward UI (P5.3 확정). class 가 build hash 라 텍스트 기반.
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
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        # 옵션 C 채택 후 Chrome PM 의존 제거 → Playwright bundled Chromium 사용 (channel 미지정).
        # WSL headless 환경 안정성 보강.
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1280, "height": 900},
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )


def is_logged_in(page: Page, tenant: TenantConfig) -> bool:
    sel = tenant.selectors
    try:
        page.wait_for_selector(sel["inbox_marker"], timeout=5_000)
        return True
    except PlaywrightTimeoutError:
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
        try:
            page.fill(sel["login_pw"], secret["login_pw"], timeout=5_000)
        except (PlaywrightTimeoutError, KeyError) as e:
            log.error("login_pw fill 실패: %s", type(e).__name__)
            return False

        page.click(sel["login_submit"])

        try:
            page.wait_for_selector(sel["otp_input"], timeout=10_000)
            code = totp_code_from_secret(secret)
            page.fill(sel["otp_input"], code)
            del code
            if sel.get("otp_submit"):
                page.click(sel["otp_submit"])
            else:
                # KIRAMS: 별도 submit 버튼 ✗ — Enter 로 폼 submit. fill() 은 input 이벤트만 발생,
                # keydown/keypress 가 필요한 사이트는 page.press 로 보강.
                page.press(sel["otp_input"], "Enter")
            try:
                page.wait_for_selector(sel["inbox_marker"], timeout=20_000)
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


def forward_via_webmail(page: Page, tenant: TenantConfig) -> None:
    """detail page 진입 상태에서 호출. 전달 → 받은편지함 자동 복귀.

    실패 시 예외 raise → caller 가 partial_failure 처리.
    """
    sel = tenant.selectors

    # detail 진입 직후 로딩 오버레이 / 직전 동작의 portal 잔재 정착 wait.
    _wait_for_ui_idle(page)

    page.locator(sel["forward_button"]).first.click(timeout=10_000)
    page.wait_for_selector(sel["forward_to_input"], timeout=15_000)

    # 제목 prefix 먼저 — chip 입력 단계에서 우발적 form submit 이 발생해도
    # prefix 가 이미 적용된 상태로 발송되는 안전망.
    subject_input = page.locator(sel["forward_subject_input"])
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


def move_to_gmail_folder(page: Page, tenant: TenantConfig) -> None:
    """받은편지함 listing 의 첫 row 를 Gmail 폴더로 이동.

    forward_via_webmail 호출 후 받은편지함 복귀 상태에서 caller 가 호출.
    실패 시 예외 raise → 다음 회차 dup forward 위험 (옵션 1: dup 허용).
    """
    sel = tenant.selectors

    # 첫 row 체크박스 click → 선택 → toolbar 활성화
    page.locator(sel["row_checkbox"]).first.click(timeout=10_000)

    page.locator(sel["toolbar_move_button"]).first.click(timeout=10_000)
    page.wait_for_selector(sel["move_dropdown"], timeout=10_000)

    page.locator(sel["move_target_gmail"]).first.click(timeout=10_000)

    # listing 갱신 — 그 row 가 사라짐. dropdown 닫힘 + 첫 row 의 mid 변동.
    # 결정적 marker 가 없어 짧은 정착 대기 + listing re-stabilize.
    page.wait_for_selector(sel["move_dropdown"], state="detached", timeout=10_000)
    page.wait_for_selector(sel["inbox_marker"], timeout=15_000)


def process_inbox(page: Page, tenant: TenantConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """받은편지함 = pending queue. 위에서부터 poll_limit 건 처리.

    각 회: 첫 row → detail → forward → 받은편지함 복귀 → Gmail 폴더 이동.
    listing 빔 또는 forward/move 실패 시 break.
    """
    sel = tenant.selectors
    forwarded: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for _ in range(tenant.poll_limit):
        _ensure_inbox(page, tenant)

        rows = page.locator('tbody tr[data-index="0"]')
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

        try:
            row.locator(sel["row_open_trigger"]).first.click(timeout=10_000)
            page.wait_for_url(tenant.detail_url_re, timeout=15_000)
        except PlaywrightTimeoutError:
            failures.append({"id": "?", "subject": subject_text, "error": "row_click_failed"})
            break

        m = tenant.detail_url_re.search(page.url)
        mid = m.group(1) if m else "?"

        try:
            forward_via_webmail(page, tenant)
        except Exception as e:
            log.exception("forward 실패: mid=%s", mid)
            failures.append({"id": mid, "subject": subject_text, "error": f"forward:{type(e).__name__}"})
            break

        try:
            move_to_gmail_folder(page, tenant)
        except Exception as e:
            log.exception("move 실패: mid=%s", mid)
            failures.append({"id": mid, "subject": subject_text, "error": f"move:{type(e).__name__}"})
            break

        forwarded.append({"id": mid, "from": from_text, "subject": subject_text})

    return forwarded, failures


def notify_telegram(text: str) -> None:
    """KIRAMS forwarding 알림은 gws-assistant 의 Gmail 브리핑에 흡수되므로 별도 Telegram 발송 ✗.

    debugging 용으로 stderr 에만 흘려둠 (OpenClaw 의 stdout-Telegram hook 회피).
    """
    log.info("notify: %s", text)


def cron_run(tenant: TenantConfig) -> Outcome:
    state = load_state()
    outcome = Outcome()

    with sync_playwright() as pw:
        ctx = open_context(pw, tenant, headless=True)
        page = ctx.new_page()
        page.goto(tenant.entry_url, wait_until="domcontentloaded", timeout=30_000)

        if not is_logged_in(page, tenant):
            if not perform_login(page, tenant):
                outcome.error = "auth_failed"
                ctx.close()
                return outcome

        try:
            outcome.forwarded, outcome.failures = process_inbox(page, tenant)
        except Exception as e:
            log.exception("process_inbox 예외")
            outcome.error = f"process_failed: {type(e).__name__}"
            ctx.close()
            return outcome

        ctx.close()

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
    if outcome.failures:
        notify_telegram(
            f"⚠ {tenant.label} {len(outcome.failures)}건 forwarding 실패 — 다음 회차 재시도"
        )
    return 0 if not outcome.failures else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="webmail-watch")
    parser.add_argument("tenant", choices=sorted(TENANTS.keys()))
    parser.add_argument("--bootstrap", action="store_true", help="1회 수동 로그인 (headed)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    tenant = TENANTS[args.tenant]

    if args.bootstrap:
        bootstrap_run(tenant)
        return 0

    outcome = cron_run(tenant)
    return report(tenant, outcome)


if __name__ == "__main__":
    sys.exit(main())
