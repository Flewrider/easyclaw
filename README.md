# EasyClaw

**Claude Code on a VPS, controllable from anywhere.**

EasyClaw turns a Linux VPS into a persistent AI assistant: Claude Code runs 24/7 in a tmux session, you chat with it via Telegram, and a web dashboard gives you a live view of the terminal, chat history, and service status — all accessible over Tailscale.

```
Your phone (Telegram) ──> clawdy-bridge ──> Claude Code (tmux)
                              |
                    Web dashboard (:8765)
                    Live terminal · Chat bubbles · Services
```

---

## Features

- **Telegram interface** — send messages, Claude replies via bot; typing indicator, voice message transcription
- **Web dashboard** — Telegram-style chat bubbles, live tmux terminal with scrollback, service management, real-time SSE updates
- **MCP tools** — Claude can call `telegram_send`, `memory_add/search`, `activity_log`, `set_status`, `send_to_peer`, and more natively
- **Memory system** — SQLite FTS5 database; Claude remembers context across restarts
- **Peer bot bridge** — two EasyClaw instances talk to each other over Tailscale
- **Auto-restart** — crash detection, graceful restarts, restart context preserved
- **Cron maintenance** — background task checks every 30 minutes while idle

---

## Quick Start

```bash
git clone https://github.com/Flewrider/easyclaw.git
cd easyclaw
chmod +x setup.sh
./setup.sh
```

The setup script walks you through everything interactively.

```bash
./setup.sh --verbose    # Show debug output in real time
```

Logs always go to `/tmp/easyclaw-setup-<timestamp>.log`.

---

## Prerequisites

```bash
sudo apt-get update
sudo apt-get install -y curl jq git python3 python3-pip nodejs npm tmux

# Install Claude Code CLI
curl -fsSL https://claude.ai/install.sh | sh
```

You also need:
- A **Telegram bot token** — create one via [@BotFather](https://t.me/BotFather)
- A **Tailscale account** (optional but strongly recommended)

---

## Authentication

During `setup.sh` you will be asked to authenticate Claude Code once.

1. Open a second terminal on the VPS
2. Run the command shown by `setup.sh`:
   ```bash
   claude --dangerously-skip-permissions
   ```
3. Complete the browser auth flow (Claude prints a URL if running headless)
4. Exit Claude and return to `setup.sh`

---

## Services

After setup, two systemd services run automatically:

| Service | Description |
|---------|-------------|
| `claude-code` | Claude Code running in a tmux session (`claude:claude`) |
| `clawdy-bridge` | Telegram bot + web dashboard + peer bridge on port 8765 |

```bash
# Status
sudo systemctl status claude-code clawdy-bridge

# Live logs
sudo journalctl -u clawdy-bridge -f

# Restart Claude
sudo systemctl restart claude-code
```

---

## Web Dashboard

Accessible at `http://<tailscale-ip>:8765`

**Chat tab** — full conversation history with Telegram-style bubbles. Source badges distinguish message origin (TG / DASH / PEER / CRON / SYS). Real-time SSE push. Scroll up to auto-load older messages.

**Terminal tab** — live tmux pane with 500 lines of scrollback. Auto-sizes to your terminal width when attached; snaps to 220x50 when you detach so the dashboard always gets clean output.

**Services tab** — status and restart buttons for all systemd services.

**Settings tab** — effort level, model display, runtime options.

---

## Configuration

`setup.sh` writes a `.env` file. Edit it to change settings:

```bash
nano .env
sudo systemctl daemon-reload
sudo systemctl restart clawdy-bridge
```

Key variables (see `default.env` for all options):

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Required — your bot token from BotFather |
| `TELEGRAM_ALLOWED_CHATS` | Comma-separated Telegram chat IDs to whitelist |
| `BRIDGE_PORT` | Dashboard + bridge HTTP port (default: 8765) |
| `BRIDGE_API_KEY` | Shared secret for peer-to-peer bridge auth |
| `PEER_BRIDGE_URL` | URL of the peer EasyClaw instance (e.g. Tailscale IP) |

---

## MCP Tools

Claude has these tools available natively via the MCP server:

| Tool | Description |
|------|-------------|
| `telegram_send` | Send a message to the Telegram owner |
| `memory_add / search / show / list` | Persistent SQLite FTS5 memory |
| `activity_log` | Append an entry to the activity log |
| `set_status` | Set `busy` or `idle` (suppresses cron during active work) |
| `send_to_peer` | Send a message to the peer bot over Tailscale |
| `reminder_set / list / cancel` | Timed reminders |
| `task_add / done / list` | Task list management |
| `converse_with_agent` | Spawn a sub-agent conversation |

---

## Peer Bot Bridge

Two EasyClaw instances can message each other over Tailscale:

```
Instance A (SuperClawdy) ──> /inject ──> Instance B (Karly)
                         <── /inject <──
```

Set `PEER_BRIDGE_URL` and `BRIDGE_API_KEY` on both sides. Messages appear in each dashboard as purple PEER bubbles and are injected into the Claude session prefixed with `[PEER from <name>]`.

---

## Memory System

Claude's memories persist in `~/.easyclaw/memories.db` (SQLite FTS5) across restarts.

```bash
clawdy-memory search <query>            # Full-text search
clawdy-memory show <id>                 # Get full content by ID
clawdy-memory list                      # Recent entries (last 7 days)
clawdy-memory add <category> <title> <content>
```

Categories: `system`, `user_preferences`, `tools`, `projects`, `learning`, `ideas`

---

## Updating

```bash
cd ~/dev/easyclaw
./update.sh
```

Pulls latest changes, syncs scripts, and restarts services.

---

## File Structure

```
easyclaw/
├── setup.sh                      # Interactive setup script
├── update.sh                     # Pull + sync + restart
├── default.env                   # All config variables with defaults
├── claude-start.sh.template      # Template for the Claude launcher
├── services/
│   ├── claude-code.service       # systemd: Claude Code in tmux
│   └── clawdy-bridge.service     # systemd: Telegram bot + dashboard
└── workspace/
    └── scripts/
        ├── clawdy-bridge.py      # Telegram bot, HTTP dashboard, peer bridge
        ├── clawdy-mcp.py         # MCP server (tools for Claude)
        ├── clawdy-restart        # Self-restart helper script
        └── clawdy-memory-*       # Memory CLI utilities
```

---

## Security

- **Tailscale** — the dashboard is intended to be accessed via Tailscale VPN only. Do not expose port 8765 to the public internet.
- **`BRIDGE_API_KEY`** — authenticates peer-to-peer `/inject` requests. Use a strong random value.
- **`--dangerously-skip-permissions`** — lets Claude run headlessly without prompts. Only use on machines you fully control.
- **`.env` permissions** — set to `chmod 600` by the setup script.

---

## Troubleshooting

**Claude is not responding to Telegram messages**
```bash
sudo journalctl -u clawdy-bridge --since "5 minutes ago"
tmux attach -t claude
```

**Dashboard shows Claude as offline**
The bridge polls `tmux has-session`. If the session died, restart `claude-code`:
```bash
sudo systemctl restart claude-code
```

**Service fails to start**
- Verify `claude` is in PATH: `which claude`
- Check `.env` has `TELEGRAM_BOT_TOKEN` set
- Check tmux is installed: `which tmux`

**Tailscale auth loop**
```bash
sudo tailscale up --reset
```

---

## License

MIT
