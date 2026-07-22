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

# ── Generation marker: retire superseded instances without killing anything ──
# A restart leaves the OLD script's poll loops (the wait loop + the dev-channels
# watcher below) running — their `tmux has-session` check misses the sub-second
# kill/recreate window, so they never exit and pile up across restarts (and would
# accumulate watchers -> duplicate Enters). Instead of hunting and killing stale
# PIDs (risky — the systemd MainPID must never be killed or it restart-loops), we
# stamp our PID here; every loop below bails the moment a NEWER instance overwrites
# this file. Stale instances self-retire within one tick. No process is ever killed.
GEN_FILE="$EASYCLAW/channels-script.pid"
echo $$ > "$GEN_FILE"

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
    $CLAUDE \"\$(cat $CTX_FILE)\" --continue --dangerously-skip-permissions --chrome \
      --mcp-config $CHANNELS_MCP \
      --channels plugin:telegram@claude-plugins-official \
      --dangerously-load-development-channels server:easyclaw-bridge \
    || $CLAUDE \"\$(cat $CTX_FILE)\" --dangerously-skip-permissions --chrome \
      --mcp-config $CHANNELS_MCP \
      --channels plugin:telegram@claude-plugins-official \
      --dangerously-load-development-channels server:easyclaw-bridge; \
    echo \"[claude exited — restarting in 3s...]\"; sleep 3; \
  done"

# ── Auto-confirm the dev-channels startup prompt ──────────────────────
# Every claude (re)launch above runs with --dangerously-load-development-channels,
# which shows a blocking confirmation:
#     ❯ 1. I am using this for local development
#       2. Exit
#     Enter to confirm · Esc to cancel
# Option 1 is pre-selected, so a bare Enter confirms it. Without this, a fresh
# start OR any crash-relaunch inside the while-loop hangs at the prompt until
# something incidentally sends an Enter (observed: hours-long stalls after a
# crash). This watcher runs for the life of the session and presses Enter ONLY
# when the prompt is actually on screen, so no stray keystrokes reach the REPL.
(
  while tmux has-session -t "$SESSION" 2>/dev/null; do
    # Retire if a newer script instance has taken over (see generation marker).
    # $$ inside this subshell is the parent script's PID, which is what we stamped.
    [ "$(cat "$GEN_FILE" 2>/dev/null)" = "$$" ] || exit 0
    if tmux capture-pane -pt "$SESSION" 2>/dev/null \
         | grep -qE "local development|Enter to confirm"; then
      tmux send-keys -t "$SESSION" Enter 2>/dev/null
      sleep 3   # let claude consume the prompt before re-checking (avoid double-send)
    fi
    sleep 1
  done
) &

# Wait for tmux session. NOTE: deliberately NO generation-retire check here — if
# this process is the systemd MainPID, exiting would trip Restart=always and cause
# a spurious restart. A lingering wait loop is harmless (pure sleep, no side effects,
# no watcher); only the dev-channels watcher above must retire to avoid duplicate
# Enters, and it does so safely because a backgrounded subshell exiting is invisible
# to systemd.
while tmux has-session -t "$SESSION" 2>/dev/null; do sleep 2; done
