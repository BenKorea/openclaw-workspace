#!/usr/bin/env bash
# ask-brain enqueue — OpenClaw(컨테이너) 측. 텔레그램 질문을 공유 마운트 큐에 job 으로 떨군다.
# 빠른 작업만(파일 1개 쓰기) → 게이트웨이 watchdog 회피. 무거운 검색·추론은 호스트 ask-brain.sh 가.
#
# usage: enqueue.sh "<질문>" ["<reply_target chat id>"]
#   reply_target 미지정 시 호스트 러너가 ASK_BRAIN_TARGET 기본값으로 회신(단일 사용자 MVP).
set -euo pipefail

Q="${1:-}"
[ -n "$Q" ] || { echo "usage: enqueue.sh \"<질문>\" [\"<chat_id>\"]"; exit 1; }
TARGET="${2:-${ASK_BRAIN_TARGET:-}}"

# 공유 마운트(~/.openclaw/workspace) 안의 런타임 큐. ~ 는 컨테이너=/home/node·호스트=/home/ben 양쪽 해석.
QDIR="$HOME/.openclaw/workspace/ask-brain-queue/jobs"
mkdir -p "$QDIR"

ID="$(date +%Y%m%dT%H%M%S)-$$"
TMP="$QDIR/.$ID.tmp"
JOB="$QDIR/$ID.json"

python3 - "$TMP" "$Q" "$TARGET" "$ID" <<'PY'
import json, sys
tmp, q, target, jid = sys.argv[1:5]
json.dump(
    {"id": jid, "question": q, "reply_channel": "telegram", "reply_target": target},
    open(tmp, "w", encoding="utf-8"), ensure_ascii=False,
)
PY

mv "$TMP" "$JOB"   # 원자적: path 워처가 완성된 *.json 만 보게 (.tmp 는 러너가 무시)
echo "enqueued: $JOB"
