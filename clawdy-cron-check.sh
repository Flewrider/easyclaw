#!/bin/bash
# Triggered every 30 min — injects task check only if Clawdy is idle
SESSION="claude"
WINDOW="claude"
STATUS_FILE="$HOME/.easyclaw/status"

# Only run if tmux session exists
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  exit 0
fi

# Skip if Clawdy is busy — but auto-clear if stale (>2 hours)
status=$(cat "$STATUS_FILE" 2>/dev/null || echo "idle")
if [ "$status" = "busy" ]; then
  last_modified=$(stat -c %Y "$STATUS_FILE" 2>/dev/null || echo 0)
  now=$(date +%s)
  age=$(( now - last_modified ))
  if [ "$age" -lt 7200 ]; then
    exit 0
  fi
  # Stale busy — reset and continue
  echo "idle" > "$STATUS_FILE"
fi

CONTEXT_FILE="$HOME/.easyclaw/restart-context"

# Write trigger context BEFORE injecting — survives any restart mid-work
echo "CRON" > "$CONTEXT_FILE"

# Inject task check with [CRON] tag — no Telegram messages, log to activity log only
tmux send-keys -t "$SESSION:$WINDOW" "[CRON] Check ~/.easyclaw/tasks.md — if there are pending or in-progress tasks, continue working on them and update their status."
sleep 1
tmux send-keys -t "$SESSION:$WINDOW" "" Enter
