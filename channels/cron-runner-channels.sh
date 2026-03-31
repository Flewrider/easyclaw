#!/bin/bash
# Cron runner for channels system — POSTs to broker instead of tmux inject.
#
# Usage: clawdy-cron-runner.sh CRON_NAME ["message content"]
# If no message provided, auto-builds from the cron's .md file.
# The broker persists the message; the channel MCP server delivers it to Claude.

CRON_NAME="${1:-HEARTBEAT}"
EASYCLAW="$HOME/.easyclaw"
CRONS_DIR="$EASYCLAW/workspace/crons"
MD_FILE="$CRONS_DIR/${CRON_NAME}.md"
BROKER_PORT="${BROKER_PORT:-7899}"
STATUS_FILE="$EASYCLAW/status"

# Build message: use explicit arg or auto-build from .md file
if [ -n "${2:-}" ]; then
    MESSAGE="$2"
elif [ -f "$MD_FILE" ]; then
    MESSAGE="[CRON - ${CRON_NAME}] Read ${MD_FILE} and follow its instructions carefully."
else
    MESSAGE="[CRON - ${CRON_NAME}] No prompt file found."
fi

# Check busy status for heartbeat crons
if [ "$CRON_NAME" = "HEARTBEAT" ] && [ -f "$STATUS_FILE" ]; then
    STATUS=$(cat "$STATUS_FILE" 2>/dev/null)
    if [ "$STATUS" = "busy" ]; then
        # Check staleness (2 hours)
        MTIME=$(stat -c %Y "$STATUS_FILE" 2>/dev/null || echo 0)
        NOW=$(date +%s)
        AGE=$(( NOW - MTIME ))
        if [ "$AGE" -lt 7200 ]; then
            exit 0  # Skip — Claude is busy
        fi
    fi
fi

# POST to broker
curl -sf --max-time 5 \
    -X POST "http://127.0.0.1:$BROKER_PORT/send" \
    -H "Content-Type: application/json" \
    -d "{
        \"source\": \"cron\",
        \"sender\": \"$CRON_NAME\",
        \"content\": $(echo "$MESSAGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),
        \"ttl_seconds\": 1800
    }" \
    > /dev/null 2>&1

exit 0
