# Easyclaw Channels Migration Plan

## Overview

Migrate from the current tmux injection queue system to Claude Code's native `--channels` feature. This replaces `clawdy-bridge.py` (Telegram polling + HTTP injection) and `pending-injections.jsonl` with MCP channel servers.

## Current Architecture

```
[Telegram API] → [clawdy-bridge.py polling] → [Python queue] → [tmux send-keys injection]
[Peer bot POST] → [clawdy-bridge.py /inject] → [Python queue] → [tmux send-keys injection]
[Cron runner]   → [HTTP POST to bridge]       → [Python queue] → [tmux send-keys injection]
```

**Services:**
- `claude-code.service` — runs `claude-start.sh` which manages tmux session
- `clawdy-bridge.service` — runs `clawdy-bridge.py` (Telegram polling, HTTP server, injection queue, dashboard)

## Target Architecture

```
[Telegram API] → [telegram plugin (MCP channel)] → [Claude Code session directly]
[Peer bot POST] → [peer-bridge channel (custom MCP)] → [Claude Code session directly]
[Cron triggers] → [cron channel (custom MCP)] → [Claude Code session directly]
```

**Services:**
- `claude-code.service` — runs Claude Code with `--channels` flag (no tmux wrapper)
- `clawdy-bridge.service` — STOPPED (replaced by telegram plugin + custom channels)
- Dashboard: kept as standalone Flask app or migrated later

## Migration Steps

### Phase 1: Install & Configure Telegram Plugin

1. Install the official Telegram plugin:
   ```bash
   claude plugin install telegram@claude-plugins-official
   ```

2. Configure with existing bot token:
   ```bash
   mkdir -p ~/.claude/channels/telegram
   echo "TELEGRAM_BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/.easyclaw/.env | cut -d= -f2)" > ~/.claude/channels/telegram/.env
   ```

3. Pre-configure access (skip pairing, directly allowlist Ben's chat IDs):
   ```bash
   # Extract chat IDs from current config
   CHAT_IDS=$(grep TELEGRAM_CHAT_ID ~/.easyclaw/.env | cut -d= -f2)
   # Write access.json with allowlist mode
   cat > ~/.claude/channels/telegram/access.json << 'EOF'
   {
     "dmPolicy": "allowlist",
     "allowFrom": ["CHAT_ID_HERE"],
     "groups": {},
     "pending": {}
   }
   EOF
   ```

### Phase 2: Build Custom Peer Bridge Channel

Create `/home/ben/dev/easyclaw/channels/peer-bridge/` — an MCP server that:
- Listens on HTTP port (Tailscale-accessible) for incoming peer messages
- Emits MCP channel notifications when messages arrive
- Exposes a `send_to_peer` tool for outbound messages
- Reads peer config from `~/.easyclaw/peers.json`

### Phase 3: Build Custom Cron Channel

Create `/home/ben/dev/easyclaw/channels/cron-system/` — an MCP server that:
- Listens on a local HTTP port for cron triggers
- Emits MCP channel notifications with cron message content
- Reads `~/.easyclaw/workspace/crons/` for cron definitions
- Respects status file (`~/.easyclaw/status`) for busy/idle gating

### Phase 4: Update Startup Script

New `claude-start.sh`:
```bash
#!/bin/bash
# Stop old services
sudo systemctl stop clawdy-bridge.service
sudo systemctl disable clawdy-bridge.service

# Start Claude Code with channels
exec claude \
  --continue \
  --dangerously-skip-permissions \
  --chrome \
  --channels \
    plugin:telegram@claude-plugins-official \
    server:peer-bridge \
    server:cron-system
```

MCP server config for custom channels (in `.mcp.json` or `--mcp-config`):
```json
{
  "mcpServers": {
    "peer-bridge": {
      "command": "python3",
      "args": ["/home/ben/dev/easyclaw/channels/peer-bridge/server.py"]
    },
    "cron-system": {
      "command": "python3",
      "args": ["/home/ben/dev/easyclaw/channels/cron-system/server.py"]
    }
  }
}
```

### Phase 5: Update systemd Service

```ini
[Unit]
Description=Claude Code with Channels
After=network.target

[Service]
Type=simple
User=ben
WorkingDirectory=/home/ben
ExecStart=/home/ben/claude-start-channels.sh
Restart=always
RestartSec=10
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
```

### Phase 6: Update CLAUDE.md Trigger Rules

Update trigger rules to match channel message format:
```
<channel source="telegram" user="Ben" chat_id="12345">message</channel>
→ Reply via the telegram channel's reply tool

<channel source="peer-bridge" sender="karly">message</channel>
→ Reply via peer-bridge's send_to_peer tool

<channel source="cron-system" cron="HEARTBEAT">message</channel>
→ Process silently, use activity_log
```

## What Gets Removed

- `clawdy-bridge.py` — Telegram polling + injection queue (replaced by telegram plugin)
- `clawdy-bridge.service` — systemd service for bridge
- `telegram-bot.py` — legacy duplicate
- `pending-injections.jsonl` — queue file (no longer needed)
- `clawdy-cron-runner.sh` — replaced by cron channel
- tmux session management in `claude-start.sh`

## What Stays (Unchanged)

- `clawdy-mcp.py` — MCP tools (memory, activity_log, set_status, tasks, reminders, spawn_agent, TOTP)
  - Remove: `telegram_send` tool (replaced by channel reply)
  - Remove: `send_to_peer` tool (replaced by peer-bridge channel tool)
  - Keep everything else
- `~/.easyclaw/memories.db` — memory system
- `~/.easyclaw/activity-log.md` — activity log
- `~/.easyclaw/tasks.md` — task tracking
- `~/.easyclaw/status` — busy/idle status
- Dashboard (can run standalone on port 5050 or integrate later)

## Rollback Plan

If migration fails:
1. `sudo systemctl stop claude-code.service`
2. `sudo systemctl start clawdy-bridge.service`
3. Run old `claude-start.sh` manually
4. Everything reverts to tmux injection system

Old files are NOT deleted — just not started.

## Deployment Order (When Ben is at Laptop)

1. `sudo systemctl stop claude-code.service` — stop current Claude
2. `sudo systemctl stop clawdy-bridge.service` — stop bridge (frees Telegram polling)
3. `claude plugin install telegram@claude-plugins-official` — install plugin
4. Configure telegram access.json with Ben's chat IDs
5. Copy new startup script
6. `sudo systemctl daemon-reload`
7. `sudo systemctl start claude-code.service` — start with channels
8. Test: send Telegram message, verify Claude responds
9. If broken: rollback (steps above)
