#!/usr/bin/env bash
# webmail-demo — KIRAMS webmail 자동로그인 시연 한방 래퍼.
#
# "kirams webmail 자동로그인 시연해줘" → 이 스크립트 한 번으로:
#   ① prod webmail 타이머 정지 (시연 중 자동발화·프로필 root 재생성 방지)
#   ② chrome-profile 권한을 uid 1000(ben) 으로
#      (sidecar 컨테이너가 root 로 만든 프로필을 호스트 native 가 쓰게)
#   ③ headed bootstrap 시연 — toml ID/PW/OTP 자동주입 → 로그인 → 받은편지함
#   ④ (창 닫으면/에러 시) prod 타이머 복구  ← trap 으로 보장
#
# 전제: /etc/sudoers.d/webmail-demo 에 NOPASSWD chown 규칙 1줄.
#   ben ALL=(root) NOPASSWD: /usr/bin/chown -R 1000\:1000 /home/ben/.openclaw/skills/webmail-watch/chrome-profile/kirams
# 없으면 ② 에서 sudo 비번 프롬프트 → 비대화식(스킬/cron)에선 실패.
# 그 경우 Dr. Ben 이 대화형 터미널에서 직접 실행(비번 입력 가능).
#
# 녹화: headed 라 Game Bar(Win+Alt+R) 또는 Win11 캡처도구(영역 녹화)로.
#       자동 webm 원하면 run.py open_context 에 record_video_dir 추가.
#
# 2026-06-15 도입.
set -euo pipefail

TIMER=openclaw-webmail-sidecar.timer
PROFILE="$HOME/.openclaw/skills/webmail-watch/chrome-profile/kirams"
SKILL_DIR="$HOME/.openclaw/workspace/skills/webmail-watch"

cleanup() {
  # ④ 어떤 경로로 끝나든 prod 타이머 복구 (시연 중 멈춰둔 것을 되돌림)
  systemctl --user start "$TIMER" 2>/dev/null || true
  echo "[demo-auto] ④ prod 타이머 복구 ($TIMER)"
}
trap cleanup EXIT

echo "[demo-auto] ① prod webmail 타이머 정지 — 시연 중 자동발화·프로필 충돌 방지"
systemctl --user stop "$TIMER" 2>/dev/null || true

echo "[demo-auto] ② 프로필 권한 → uid 1000 (NOPASSWD chown)"
sudo chown -R 1000:1000 "$PROFILE"

echo "[demo-auto] ③ headed bootstrap 시연 — ID/PW/OTP 자동주입 → 로그인 → 받은편지함"
echo "[demo-auto]    (창을 닫으면 시연 종료 + 타이머 자동 복구)"
cd "$SKILL_DIR"
uv run run.py kirams --bootstrap
