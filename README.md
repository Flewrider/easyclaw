# EasyClaw

Deploy Claude Code on a VPS with a Telegram bot interface, systemd services, and automated setup.

**Tested on:** Ubuntu 22.04 LTS
**Runs as:** a non-root user with `sudo` access

---

## Quick Start

```bash
git clone https://github.com/yourusername/easyclaw.git
cd easyclaw
chmod +x setup.sh
./setup.sh
```

The script will walk you through each step interactively.

### Debug Mode

```bash
./setup.sh --verbose    # Show real-time debug output to stderr
```

The setup process logs everything to `/tmp/easyclaw-setup-<timestamp>.log` regardless of verbose mode.

---

## Prerequisites

Install these before running `setup.sh`:

```bash
sudo apt-get update
sudo apt-get install -y curl jq git python3 python3-pip nodejs npm

# Claude Code CLI — follow the official install guide:
# https://claude.ai/download
```

---

## Manual Browser Auth Steps

During `setup.sh` you will be prompted to run Claude Code once for authentication.
This step creates `~/.claude/claude.json` with your OAuth credentials.

**Step-by-step:**

1. Open a second terminal on your VPS (same user, same machine).

2. Run the command shown by `setup.sh`:
   ```
   claude --session-id <ID> --dangerously-skip-permissions --browser
   ```

3. Claude will open a browser tab (or print a URL). Complete the OAuth flow.

4. **Interactive prompts you may encounter:**

   | Prompt | Action |
   |--------|--------|
   | "Trust this directory?" | Press Enter or arrow keys to select Yes, then Enter |
   | Model selection list | Arrow keys to select, Enter to confirm |
   | Terms of service | Read and accept |
   | Browser auth URL | Open in browser, sign in, paste code back if prompted |

5. Once authenticated, type `exit` or press `Ctrl-C` in Claude.

6. Return to the `setup.sh` terminal and press Enter to continue.

**If the browser flag does not work on a headless VPS:**

```bash
# Option A: print-only URL mode (copy/paste URL to local browser)
claude --session-id <ID> --dangerously-skip-permissions

# Option B: set DISPLAY if you have X forwarding
DISPLAY=:0 claude --session-id <ID> --dangerously-skip-permissions --browser
```

---

## Configuration

`setup.sh` writes a `.env` file with your settings. You can edit it later:

```
nano .env
sudo systemctl daemon-reload
sudo systemctl restart claude-code telegram-bot
```

See `env.example` for all available variables.

---

## Services

| Service | Description |
|---------|-------------|
| `claude-code` | Claude Code daemon (auto-restarts on failure) |
| `telegram-bot` | Telegram bot bridge (auto-restarts on failure) |

Common commands:

```bash
# Status
sudo systemctl status claude-code telegram-bot

# Logs (live)
sudo journalctl -u claude-code -f
sudo journalctl -u telegram-bot -f

# Restart
sudo systemctl restart claude-code

# Disable autostart
sudo systemctl disable claude-code
```

---

## Security

- **UFW firewall** — `setup.sh` can configure UFW to allow only SSH (22) and
  Tailscale (41641 UDP). All other inbound ports are blocked by default.
- **Tailscale** — optional VPN for accessing the VPS without exposing it to the
  public internet. `setup.sh` offers to install and configure it.
- **`--dangerously-skip-permissions`** — this flag lets Claude Code run headlessly
  without interactive prompts. Only use it on machines you control and trust.
- **`.env` permissions** — `setup.sh` sets `.env` to `chmod 600` so only your
  user can read the bot token.

---

## File Structure

```
easyclaw/
├── setup.sh              # Main setup script
├── env.example           # Environment variable template
├── README.md             # This file
└── services/
    ├── claude-code.service   # systemd unit for Claude Code
    └── telegram-bot.service  # systemd unit for Telegram bot
```

---

## Troubleshooting

**`claude.json` not created after auth step**

Re-run `setup.sh` — it will detect the missing file and prompt you to retry.
Alternatively, create a minimal stub manually:
```bash
echo '{"trusted_paths": ["/home/YOUR_USER"]}' > ~/.claude/claude.json
```

**Service fails to start immediately**

Check logs:
```bash
sudo journalctl -u claude-code --since "5 minutes ago"
```

Common causes:
- `claude` binary not in `PATH` for the system user — verify with `which claude`
- `.env` file missing or malformed — check `TELEGRAM_BOT_TOKEN` is set
- Session ID expired — re-run `setup.sh` to regenerate

**Tailscale auth loop**

```bash
sudo tailscale up --reset
```

---

## Re-running Setup

`setup.sh` is idempotent for most steps:

- **Session ID**: reused if `~/.claude/session-id` already exists
- **claude.json patch**: skipped if home dir already in `trusted_paths`
- **Services**: overwritten and reloaded on each run
- **UFW / Tailscale**: prompts before making changes

---

## License

MIT
