#!/bin/bash
# Triggered every 30 min — injects task check only if Clawdy is idle
SESSION="claude"
WINDOW="claude"
STATUS_FILE="$HOME/.easyclaw/status"

# Load TOKEN and CHAT_ID from .env
TOKEN=""
CHAT_ID=""
if [ -f "$HOME/.easyclaw/.env" ]; then
  while IFS='=' read -r key value; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    case "$key" in
      TELEGRAM_BOT_TOKEN) TOKEN="$value" ;;
      TELEGRAM_CHAT_ID) CHAT_ID="$value" ;;
    esac
  done < "$HOME/.easyclaw/.env"
fi

send_telegram() {
  local msg="$1"
  if [ -n "$TOKEN" ] && [ -n "$CHAT_ID" ]; then
    curl -s --max-time 10 \
      "https://api.telegram.org/bot${TOKEN}/sendMessage" \
      -d "chat_id=${CHAT_ID}" \
      --data-urlencode "text=${msg}" > /dev/null
  fi
}

# Only run if tmux session exists
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  send_telegram "⚠️ Clawdy session is down — tmux session 'claude' not found. Restart with: sudo systemctl restart claude-code"
  exit 1
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

# Check for pane inactivity (soft warning only)
pane_last_used=$(tmux display-message -t "$SESSION:$WINDOW" -p "#{pane_last_used}" 2>/dev/null)
if [ -n "$pane_last_used" ] && [ "$pane_last_used" -gt 0 ] 2>/dev/null; then
  now=$(date +%s)
  pane_age=$(( now - pane_last_used ))
  current_status=$(cat "$STATUS_FILE" 2>/dev/null || echo "idle")
  if [ "$pane_age" -gt 10800 ] && [ "$current_status" = "idle" ]; then
    send_telegram "⚠️ Clawdy may be stuck — no tmux pane activity for 3+ hours (status: idle). Check with: tmux attach -t claude"
  fi
fi

# Prune old received files (>7 days) to prevent disk fill
if [ -d "$HOME/telegram-files" ]; then
  find "$HOME/telegram-files" -type f -mtime +7 -delete 2>/dev/null || true
fi

# Inject task check with [CRON] tag — no Telegram messages, log to activity log only
CRON_TS=$(date '+%Y-%m-%d %H:%M')
tmux send-keys -t "$SESSION:$WINDOW" "[CRON | ${CRON_TS}] Check ~/.easyclaw/tasks.md — if there are pending or in-progress tasks, continue working on them and update their status."
sleep 1
tmux send-keys -t "$SESSION:$WINDOW" "" Enter
