#!/bin/bash
# Generic cron runner — injects [CRON - NAME] message referencing the cron's .md file.
# Usage: clawdy-cron-runner.sh <CRON_NAME>
NAME="${1:-HEARTBEAT}"
CRONS_DIR="$HOME/.easyclaw/workspace/crons"
MD_FILE="$CRONS_DIR/${NAME}.md"
ENV_FILE="$HOME/.easyclaw/.env"
SESSION="claude"
WINDOW="claude"

BRIDGE_PORT=$(grep "^BRIDGE_PORT=" "$ENV_FILE" 2>/dev/null | cut -d= -f2)
BRIDGE_PORT="${BRIDGE_PORT:-8765}"

# Skip if Claude session not alive
if ! tmux has-session -t "$SESSION" 2>/dev/null; then exit 1; fi

# Skip if status is busy (and not stale)
STATUS_FILE="$HOME/.easyclaw/status"
status=$(cat "$STATUS_FILE" 2>/dev/null || echo "idle")
if [ "$status" = "busy" ]; then
  last_modified=$(stat -c %Y "$STATUS_FILE" 2>/dev/null || echo 0)
  age=$(( $(date +%s) - last_modified ))
  [ "$age" -lt 7200 ] && exit 0
  echo "idle" > "$STATUS_FILE"
fi

if [ ! -f "$MD_FILE" ]; then
  echo "No prompt file found at $MD_FILE" >&2; exit 1
fi

MSG="[CRON - ${NAME}] Read ${MD_FILE} and follow its instructions carefully."

PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'message':sys.argv[1],'source':'cron','sender':'cron'}))" "$MSG")
if ! curl -sf --max-time 5 -X POST "http://127.0.0.1:$BRIDGE_PORT/chat" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" > /dev/null 2>&1; then
  CRON_TS=$(date '+%Y-%m-%d %H:%M')
  tmux send-keys -t "$SESSION:$WINDOW" "[CRON - ${NAME} | ${CRON_TS}] ${MSG}"
  sleep 1
  tmux send-keys -t "$SESSION:$WINDOW" "" Enter
fi
