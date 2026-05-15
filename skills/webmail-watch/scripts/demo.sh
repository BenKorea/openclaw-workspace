#!/usr/bin/env bash
# webmail-watch demo — headed + slow_mo + Playwright trace.
# 모든 단계를 시각으로 따라가고 사후 review 도 가능.
#
# 사용:
#   bash scripts/demo.sh                            # 기본 (slow_mo=800ms)
#   DEMO_SLOW_MO=1500 bash scripts/demo.sh          # 더 천천히
#   DEMO_TENANT=kirams bash scripts/demo.sh         # tenant 지정 (default kirams)
#
# 출력:
#   - stdout (live)
#   - /tmp/webmail-demo.log (영속)
#   - /tmp/webmail-trace.zip (Playwright trace, `npx playwright show-trace` 로 시각 review)
#
# 보안: trace.zip 의 snapshot 에 KIRAMS 메일 본문·발신자·일부 OTP frame 캡처 가능 —
#       외부 공유 ✗, Dr. Ben 본인 신뢰 환경만.
#
# 2026-05-15 도입 (PROGRESS P8.3).

set -u
export PYTHONUNBUFFERED=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$SKILL_ROOT"
uv run python "$SCRIPT_DIR/demo.py" 2>&1 | tee /tmp/webmail-demo.log
