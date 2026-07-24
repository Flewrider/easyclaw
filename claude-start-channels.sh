#!/bin/bash
# Claude Code with Channels — location-independent startup.
# Uses $HOME to resolve all paths. Works on any machine.

export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
CLAUDE="$(which claude)"
EASYCLAW="$HOME/.easyclaw"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION="claude"

# ── Stop old services ─────────────────────────────────────────────────
sudo systemctl stop clawdy-bridge.service 2>/dev/null
sudo systemctl disable clawdy-bridge.service 2>/dev/null

# ── Ensure Telegram plugin is configured ──────────────────────────────
TELEGRAM_STATE="$HOME/.claude/channels/telegram"
mkdir -p "$TELEGRAM_STATE"
if [ ! -f "$TELEGRAM_STATE/.env" ]; then
    TOKEN=$(grep "^TELEGRAM_BOT_TOKEN=" "$EASYCLAW/.env" 2>/dev/null | cut -d= -f2)
    if [ -n "$TOKEN" ]; then
        echo "TELEGRAM_BOT_TOKEN=$TOKEN" > "$TELEGRAM_STATE/.env"
        chmod 600 "$TELEGRAM_STATE/.env"
    fi
fi
if [ ! -f "$TELEGRAM_STATE/access.json" ]; then
    CHAT_IDS=$(grep "^TELEGRAM_CHAT_ID=" "$EASYCLAW/.env" 2>/dev/null | cut -d= -f2)
    ALLOW_JSON=$(python3 -c "
import json
ids = \"$CHAT_IDS\".split(\",\")
ids = [i.strip() for i in ids if i.strip()]
print(json.dumps(ids))
" 2>/dev/null)
    cat > "$TELEGRAM_STATE/access.json" << EOFJ
{"dmPolicy":"allowlist","allowFrom":$ALLOW_JSON,"groups":{},"pending":{},"ackReaction":"eyes"}
EOFJ
fi

# ── MCP config for easyclaw-bridge channel ────────────────────────────
CHANNELS_MCP="/tmp/easyclaw-channels-mcp.json"
cat > "$CHANNELS_MCP" << EOFM
{
  "mcpServers": {
    "easyclaw-bridge": {
      "command": "python3",
      "args": ["$REPO_DIR/channels/easyclaw-bridge/channel.py"]
    }
  }
}
EOFM

# ── Setup ─────────────────────────────────────────────────────────────
mkdir -p "$EASYCLAW/logs"
echo "idle" > "$EASYCLAW/status"

# ── Determine restart context ─────────────────────────────────────────
RESUME="$EASYCLAW/restart-resume"
CTX_FILE="$EASYCLAW/current-context"
if [ -f "$RESUME" ]; then
    cp "$RESUME" "$CTX_FILE"
    rm -f "$RESUME"
else
    echo "RESTART CONTEXT: you crashed unexpectedly. Check ~/.easyclaw/activity-log.md for recent work, find the cause if possible, then continue where you left off." > "$CTX_FILE"
fi

# ── Kill stale session ───────────────────────────────────────────────
tmux kill-session -t "$SESSION" 2>/dev/null

# ── Start Claude in tmux with channels ───────────────────────────────
echo "[channels] Starting Claude Code with channels..."
echo "[channels] Telegram: plugin:telegram@claude-plugins-official"
echo "[channels] Bridge:   server:easyclaw-bridge"
echo "[channels] Repo:     $REPO_DIR"

tmux new-session -d -s "$SESSION" -c "$HOME" -n "claude" \
  "while true; do \
    (sleep 5 && tmux send-keys -t claude Enter 2>/dev/null) & \
    $CLAUDE \"\$(cat $CTX_FILE)\" --continue --dangerously-skip-permissions --chrome \
      --mcp-config $CHANNELS_MCP \
      --channels plugin:telegram@claude-plugins-official \
      --dangerously-load-development-channels server:easyclaw-bridge \
    || { (sleep 5 && tmux send-keys -t claude Enter 2>/dev/null) & \
    $CLAUDE \"\$(cat $CTX_FILE)\" --dangerously-skip-permissions --chrome \
      --mcp-config $CHANNELS_MCP \
      --channels plugin:telegram@claude-plugins-official \
      --dangerously-load-development-channels server:easyclaw-bridge; }; \
    echo \"[claude exited — restarting in 3s...]\"; sleep 3; \
  done"

# ── Startup safety-net for the dev-channels prompt ────────────────────
# The in-loop `sleep 5 && send Enter` above is a ONE-SHOT at a fixed 5s. If claude
# isn't at the prompt yet (slow start / big --continue context reload), that keystroke
# is wasted and the session hangs at the prompt indefinitely (observed: hours-long
# stall after the 2026-07-21 crash). This retries until the prompt is actually gone.
#
# Two hard rules learned from the 2026-07-24 churn:
#  1. STRICT match — require BOTH option lines. Matching loose text like
#     "Enter to confirm" alone fires on any REPL output that merely *mentions* the
#     prompt (e.g. me messaging Ben about it), injecting stray Enters into a live
#     session. That feedback loop is what caused the restart churn.
#  2. BOUNDED — give up after STARTUP_CONFIRM_SECS. A watcher that lives for the
#     whole session can always be tripped later; the in-loop Enter covers relaunches.
STARTUP_CONFIRM_SECS=90
(
  _deadline=$(( SECONDS + STARTUP_CONFIRM_SECS ))
  while [ "$SECONDS" -lt "$_deadline" ]; do
    tmux has-session -t "$SESSION" 2>/dev/null || exit 0
    _pane=$(tmux capture-pane -pt "$SESSION" 2>/dev/null)
    if printf '%s' "$_pane" | grep -q "1\. I am using this for local development" \
       && printf '%s' "$_pane" | grep -q "2\. Exit"; then
      tmux send-keys -t "$SESSION" Enter 2>/dev/null
      sleep 3                      # let claude consume it, then confirm it cleared
      continue
    fi
    sleep 1
  done
) &

# Wait for tmux session
while tmux has-session -t "$SESSION" 2>/dev/null; do sleep 2; done
