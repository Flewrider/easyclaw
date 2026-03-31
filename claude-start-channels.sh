#!/bin/bash
# Claude Code with Channels — replaces tmux-based claude-start.sh
# Two channels: official Telegram plugin + custom easyclaw-bridge (peer + cron)
# Broker daemon handles message persistence for easyclaw-bridge.

export PATH="/home/ben/.local/bin:$PATH"
CLAUDE="/home/ben/.local/bin/claude"
EASYCLAW="/home/ben/.easyclaw"

# ── Stop old services ─────────────────────────────────────────────────
sudo systemctl stop clawdy-bridge.service 2>/dev/null
sudo systemctl disable clawdy-bridge.service 2>/dev/null
echo "[channels] Old bridge service stopped"

# ── Ensure Telegram plugin is configured ──────────────────────────────
TELEGRAM_STATE="$HOME/.claude/channels/telegram"
mkdir -p "$TELEGRAM_STATE"

# Copy bot token from easyclaw .env if not already in channel config
if [ ! -f "$TELEGRAM_STATE/.env" ]; then
    TOKEN=$(grep "^TELEGRAM_BOT_TOKEN=" "$EASYCLAW/.env" 2>/dev/null | cut -d= -f2)
    if [ -n "$TOKEN" ]; then
        echo "TELEGRAM_BOT_TOKEN=$TOKEN" > "$TELEGRAM_STATE/.env"
        chmod 600 "$TELEGRAM_STATE/.env"
        echo "[channels] Telegram token configured"
    fi
fi

# Pre-configure access allowlist from existing chat IDs
if [ ! -f "$TELEGRAM_STATE/access.json" ]; then
    CHAT_IDS=$(grep "^TELEGRAM_CHAT_ID=" "$EASYCLAW/.env" 2>/dev/null | cut -d= -f2)
    ALLOW_JSON=$(python3 -c "
import json
ids = '$CHAT_IDS'.split(',')
ids = [i.strip() for i in ids if i.strip()]
print(json.dumps(ids))
" 2>/dev/null)
    cat > "$TELEGRAM_STATE/access.json" << EOFJ
{
  "dmPolicy": "allowlist",
  "allowFrom": $ALLOW_JSON,
  "groups": {},
  "pending": {},
  "ackReaction": "eyes"
}
EOFJ
    echo "[channels] Telegram access configured: $ALLOW_JSON"
fi

# ── MCP config for easyclaw-bridge channel ────────────────────────────
CHANNELS_MCP="/tmp/easyclaw-channels-mcp.json"
cat > "$CHANNELS_MCP" << 'EOFM'
{
  "mcpServers": {
    "easyclaw-bridge": {
      "command": "python3",
      "args": ["/home/ben/dev/easyclaw/channels/easyclaw-bridge/channel.py"]
    }
  }
}
EOFM

# ── Ensure log directory ─────────────────────────────────────────────
mkdir -p "$EASYCLAW/logs"

# ── Set idle status ──────────────────────────────────────────────────
echo "idle" > "$EASYCLAW/status"

# ── Determine restart context ─────────────────────────────────────────
RESUME="$EASYCLAW/restart-resume"
if [ -f "$RESUME" ]; then
    CONTEXT=$(cat "$RESUME")
    rm -f "$RESUME"
else
    CONTEXT="RESTART CONTEXT: you crashed unexpectedly. Check ~/.easyclaw/activity-log.md for recent work, find the cause if possible, then continue where you left off."
fi

# ── Launch Claude Code with channels ──────────────────────────────────
echo "[channels] Starting Claude Code with channels..."
echo "[channels] Telegram: plugin:telegram@claude-plugins-official (via --channels)"
echo "[channels] Bridge:   server:easyclaw-bridge (via --dangerously-load-development-channels)"
echo "[channels] Context:  ${CONTEXT:0:80}..."

# --channels: for approved plugins (telegram)
# --dangerously-load-development-channels: for custom server: entries (easyclaw-bridge)
#   server: entries require this flag to bypass the allowlist check.
exec $CLAUDE "$CONTEXT" \
    --continue \
    --dangerously-skip-permissions \
    --chrome \
    --mcp-config "$CHANNELS_MCP" \
    --channels \
        plugin:telegram@claude-plugins-official \
    --dangerously-load-development-channels \
        server:easyclaw-bridge
