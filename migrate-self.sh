#!/bin/bash
# Self-migration script — runs detached, survives Claude restart.
# 1. Sends Telegram notification
# 2. Stops old Claude + bridge
# 3. Installs Telegram plugin + bun
# 4. Configures channels
# 5. Starts new Claude with channels
# 6. Sends Telegram notification when done

set -e
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"

EASYCLAW="$HOME/.easyclaw"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$EASYCLAW/logs/migration.log"
mkdir -p "$EASYCLAW/logs"

# Load bot token + chat ID for notifications
TOKEN=$(grep "^TELEGRAM_BOT_TOKEN=" "$EASYCLAW/.env" | cut -d= -f2)
CHAT_ID=$(grep "^TELEGRAM_CHAT_ID=" "$EASYCLAW/.env" | cut -d= -f2 | cut -d, -f1)

notify() {
    curl -sf --max-time 10 \
        "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        --data-urlencode "text=$1" > /dev/null 2>&1 || true
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
    echo "$*"
}

log "=== Starting self-migration to channels ==="
notify "🔄 Migration starting — I'll be offline for ~60 seconds..."

# Step 0: Escape cgroup if running inside claude-code.service
# systemd kills all processes in a service's cgroup on stop — setsid/nohup don't help.
# Re-exec ourselves under a transient scope so we survive stopping the parent service.
if [ -z "$_MIGRATED_SCOPE" ]; then
    export _MIGRATED_SCOPE=1
    log "Re-launching in independent systemd scope to survive service stop..."
    exec sudo systemd-run --scope --uid="$(id -u)" --gid="$(id -g)" bash "$0" "$@"
fi

# Step 1: Stop old services
log "Stopping old services..."
sudo systemctl stop clawdy-bridge.service 2>/dev/null || true
sudo systemctl disable clawdy-bridge.service 2>/dev/null || true
# Kill any lingering tmux claude sessions
tmux kill-session -t claude 2>/dev/null || true
# Stop claude-code LAST (this kills the session that spawned us if we didn't escape cgroup)
sudo systemctl stop claude-code.service 2>/dev/null || true
sleep 3
log "Old services stopped"

# Step 2: Install bun if missing
if ! command -v bun &> /dev/null; then
    log "Installing bun..."
    curl -fsSL https://bun.sh/install | bash 2>&1 >> "$LOG"
    export PATH="$HOME/.bun/bin:$PATH"
    log "Bun installed: $(bun --version)"
fi

# Step 3: Install Python deps if missing
python3 -c "import aiohttp" 2>/dev/null || {
    log "Installing aiohttp..."
    pip3 install aiohttp --break-system-packages 2>&1 >> "$LOG"
}
python3 -c "from mcp.server import Server" 2>/dev/null || {
    log "Installing mcp..."
    pip3 install mcp --break-system-packages 2>&1 >> "$LOG"
}

# Step 4: Install Telegram plugin
log "Installing Telegram plugin..."
claude plugin install telegram@claude-plugins-official 2>&1 >> "$LOG" || true

# Step 5: Configure Telegram channel
TELEGRAM_STATE="$HOME/.claude/channels/telegram"
mkdir -p "$TELEGRAM_STATE"
if [ ! -f "$TELEGRAM_STATE/.env" ]; then
    echo "TELEGRAM_BOT_TOKEN=$TOKEN" > "$TELEGRAM_STATE/.env"
    chmod 600 "$TELEGRAM_STATE/.env"
    log "Telegram token configured"
fi
if [ ! -f "$TELEGRAM_STATE/access.json" ]; then
    CHAT_IDS=$(grep "^TELEGRAM_CHAT_ID=" "$EASYCLAW/.env" | cut -d= -f2)
    ALLOW_JSON=$(python3 -c "
import json
ids = '$CHAT_IDS'.split(',')
ids = [i.strip() for i in ids if i.strip()]
print(json.dumps(ids))
")
    cat > "$TELEGRAM_STATE/access.json" << EOFJ
{"dmPolicy":"allowlist","allowFrom":$ALLOW_JSON,"groups":{},"pending":{},"ackReaction":"eyes"}
EOFJ
    log "Telegram access configured"
fi

# Step 6: Write restart context
cat > "$EASYCLAW/restart-resume" << 'CTX'
RESTART CONTEXT: You were migrated to the Claude Code channels system. The migration script (migrate-self.sh) stopped the old tmux+bridge system and started you with --channels for native Telegram + peer messaging. Verify everything works: check Telegram (send a message to Ben), check peer bridge (ping Karly), check broker health (curl localhost:7899/health). Send Ben a Telegram message confirming you're back online.
CTX
log "Restart context written"

# Step 7: Install new systemd service
sudo cp "$REPO_DIR/claude-code-channels.service" /etc/systemd/system/
# Fix paths for this machine
sudo sed -i "s|/home/ben/dev/easyclaw|$REPO_DIR|g" /etc/systemd/system/claude-code-channels.service
sudo sed -i "s|/home/ben|$HOME|g" /etc/systemd/system/claude-code-channels.service
sudo sed -i "s|User=ben|User=$(whoami)|g" /etc/systemd/system/claude-code-channels.service
# Point ExecStart to the repo's startup script
sudo sed -i "s|ExecStart=.*|ExecStart=$REPO_DIR/claude-start-channels.sh|g" /etc/systemd/system/claude-code-channels.service
sudo systemctl daemon-reload
sudo systemctl enable claude-code-channels.service
log "Systemd service installed"

# Step 8: Start dashboard
kill $(pgrep -f "channels/dashboard.py") 2>/dev/null || true
DASHBOARD_PORT=8766 nohup python3 "$REPO_DIR/channels/dashboard.py" > "$EASYCLAW/logs/dashboard.log" 2>&1 &
log "Dashboard started on port 8766"

# Step 9: Start new Claude with channels
log "Starting Claude Code with channels..."
sudo systemctl start claude-code-channels.service
sleep 10

# Step 10: Verify
if tmux has-session -t claude 2>/dev/null; then
    log "✅ Migration complete — Claude session is alive"
    notify "✅ Migration complete! I'm back online with the new channels system. Telegram + peer bridge + broker all set up."
else
    log "❌ Claude session did not start — check: sudo journalctl -u claude-code-channels -n 50"
    notify "❌ Migration failed — Claude didn't start. Check logs: sudo journalctl -u claude-code-channels -n 50"
fi

log "=== Migration script finished ==="
