"""webmail-watch demo mode — 시각 검증 + Playwright trace 사후 review.

bootstrap_run 의 perform_login 만이 아니라 process_inbox (forward + move) 까지
모든 단계를 headed + slow_mo 로 진행. 각 단계 print 로그. trace.zip 저장.

- headed Chromium 창이 WSLg 위에 뜸
- slow_mo (default 800ms) — 각 Playwright action 사이 대기
- Dr. Ben 이 화면 보면서 어느 단계가 fail 하는지 시각 확인
- 종료 후 /tmp/webmail-trace.zip — `npx playwright show-trace` 로 사후 review

용도:
- 셋업 시 첫 cookies 정착 + 시각 검증 (bootstrap 의 확장판)
- 향후 KIRAMS UI 변경 시 selector stale 진단 (`forward_button` 등 selector 가
  어디서 어긋나는지 trace 의 screenshot/DOM snapshot 으로 결정적 확인)
- 운영 매뉴얼·교육 자료

환경 변수:
  DEMO_SLOW_MO=800   # 각 action 대기 (ms). 더 천천히 보고 싶으면 1500
  DEMO_TENANT=kirams # tenant key

2026-05-15 도입 (PROGRESS P8.3).
"""

import os
import pathlib
import sys
import traceback

# scripts/demo.py 의 부모 디렉토리(skill root)에서 run.py import
SKILL_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))

from playwright.sync_api import sync_playwright

from run import (  # type: ignore
    PROFILE_ROOT,
    TENANTS,
    is_logged_in,
    perform_login,
    process_inbox,
)


def main() -> int:
    tenant_key = os.environ.get("DEMO_TENANT", "kirams")
    slow_mo = int(os.environ.get("DEMO_SLOW_MO", "800"))
    tenant = TENANTS[tenant_key]

    print(f"[demo] tenant: {tenant.label} ({tenant_key})")
    print(f"[demo] slow_mo: {slow_mo}ms per action")
    print(f"[demo] profile: {PROFILE_ROOT / tenant_key}")
    print(f"[demo] entry:   {tenant.entry_url}")
    print()

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_ROOT / tenant_key),
            headless=False,
            slow_mo=slow_mo,
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        # Playwright tracing — 사후 review 용
        ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
        print("[demo] tracing started")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        try:
            # === Step 1 — 로그인 페이지 이동 ===
            print("[demo] Step 1 — entry URL 이동")
            page.goto(tenant.entry_url, wait_until="domcontentloaded", timeout=30_000)

            # === Step 2 — 로그인 (이미 trusted 면 skip) ===
            print("[demo] Step 2 — 로그인 상태 확인")
            if is_logged_in(page, tenant):
                print("[demo]   이미 로그인 상태 (chrome-profile cookies 재사용)")
            else:
                print("[demo]   로그인 안 됨 — perform_login 자동 실행")
                ok = perform_login(page, tenant)
                if ok:
                    print("[demo]   로그인 통과 — 받은편지함 도달")
                else:
                    print("[demo]   ⚠ 로그인 실패 — auth_failed")
                    print("[demo]   화면 직접 확인하시고 창 닫으세요")
                    page.wait_for_event("close", timeout=0)
                    return 1

            # === Step 3 — process_inbox (forward + move 반복) ===
            print("[demo] Step 3 — process_inbox (받은편지함 처리)")
            try:
                forwarded, failures = process_inbox(page, tenant)
                print(f"[demo]   forwarded: {len(forwarded)}")
                for f in forwarded:
                    print(f"[demo]     OK   mid={f.get('id')} from={f.get('from')!r} subj={f.get('subject')!r}")
                print(f"[demo]   failures: {len(failures)}")
                for f in failures:
                    print(f"[demo]     FAIL mid={f.get('id')} error={f.get('error')!r} subj={f.get('subject')!r}")
            except Exception as exc:
                print(f"[demo]   ⚠ process_inbox 예외: {type(exc).__name__}: {exc}")
                traceback.print_exc()

            print()
            print("[demo] 모든 단계 종료 — 화면 검토 후 창 닫으시면 trace 저장됩니다")
            page.wait_for_event("close", timeout=0)

        finally:
            trace_path = "/tmp/webmail-trace.zip"
            try:
                ctx.tracing.stop(path=trace_path)
                print(f"[demo] trace 저장: {trace_path}")
                print(f"[demo] 사후 review: npx playwright show-trace {trace_path}")
            except Exception as exc:
                # Playwright 자체 race (artifact 정리 vs zip) — trace 자체는 보통 정상 저장
                print(f"[demo] trace 저장 시 Playwright 내부 race (무해): {type(exc).__name__}")
            ctx.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
