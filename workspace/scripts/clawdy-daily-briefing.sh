#!/bin/bash
# Triggered by cron at 8am — injects briefing prompt into Claude tmux session
SESSION="claude"
WINDOW="claude"
STATUS_FILE="$HOME/.easyclaw/status"
LOG_FILE="$HOME/.easyclaw/activity-log.md"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  exit 0
fi

# Get yesterday's date
YESTERDAY=$(date -d "yesterday" '+%Y-%m-%d')
TODAY=$(date '+%Y-%m-%d')

# Extract last 24h of activity from log
ACTIVITY=$(grep -E "^\[($YESTERDAY|$TODAY)" "$LOG_FILE" 2>/dev/null | tail -50)

if [ -z "$ACTIVITY" ]; then
  ACTIVITY="No activity logged."
fi

PROMPT="[DAILY BRIEFING] Good morning. Compose and send a daily briefing to the GROUP CHAT (chat_id=-5156007644) via telegram_send summarising what you did in the last 24 hours. Here is your activity log from that period:

$ACTIVITY

Also include: any pending tasks from ~/.easyclaw/tasks.md, VPS Clawdy status (ping via send_to_peer if needed), and anything Ben should know. Keep it concise. Send to the group chat only — use telegram_send with chat_id=-5156007644."

tmux send-keys -t "$SESSION:$WINDOW" "$PROMPT"
sleep 1
tmux send-keys -t "$SESSION:$WINDOW" "" Enter
