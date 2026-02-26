#!/usr/bin/env python3
"""
Clawdy Telegram Bot Bridge
- Polls Telegram for new messages from authorized chats
- Injects them into the tmux Claude session
- First message from any chat triggers an approval flow
- Run as a systemd service: clawdy-telegram-bot.service
"""

import os
import sys
import json
import time
import subprocess
import requests
import logging
from pathlib import Path
from datetime import datetime

ENV_FILE = Path.home() / ".claude" / "memory" / ".env"
CONFIG_FILE = Path.home() / ".claude" / "memory" / "telegram-config.json"
LOG_FILE = Path.home() / ".claude" / "memory" / "telegram-bot.log"
TYPING_PID_FILE = Path.home() / ".claude" / "memory" / "telegram-typing.pid"
TYPING_LOOP = Path.home() / ".claude" / "memory" / "clawdy-typing-loop.py"
TMUX_SESSION = "claude"
TMUX_WINDOW = "claude"
RESTART_CONTEXT = Path.home() / ".claude" / "restart-context"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


def load_env():
    env = {}
    if not ENV_FILE.exists():
        log.error(f"No .env file found at {ENV_FILE}")
        log.error(f"Copy .env.template to .env and set your TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"allowed_chats": [], "pending_approval": []}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)


def tg_request(token, method, **kwargs):
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=kwargs, timeout=35)
        return r.json()
    except Exception as e:
        log.error(f"Telegram API error ({method}): {e}")
        return {"ok": False}


def send_message(token, chat_id, text, **kwargs):
    return tg_request(token, "sendMessage", chat_id=chat_id, text=text, **kwargs)


def start_typing(chat_id):
    """Start background typing indicator loop."""
    stop_typing()  # kill any existing loop first
    proc = subprocess.Popen(
        [sys.executable, str(TYPING_LOOP), str(chat_id)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    TYPING_PID_FILE.write_text(str(proc.pid))
    log.info(f"Typing indicator started (PID {proc.pid})")

def stop_typing():
    """Stop the typing indicator loop."""
    if TYPING_PID_FILE.exists():
        try:
            pid = int(TYPING_PID_FILE.read_text().strip())
            os.kill(pid, 9)
            log.info(f"Typing indicator stopped (PID {pid})")
        except (ProcessLookupError, ValueError):
            pass
        TYPING_PID_FILE.unlink(missing_ok=True)

def inject_to_claude(message_text, sender_name, chat_id):
    """Inject a message into the tmux Claude session."""
    display = f"[TELEGRAM from {sender_name}]: {message_text}"
    log.info(f"Injecting to Claude: {display[:80]}")

    # Write trigger context BEFORE injecting — survives any restart mid-work
    try:
        RESTART_CONTEXT.write_text("TELEGRAM")
    except Exception as e:
        log.warning(f"Could not write restart context: {e}")

    try:
        subprocess.run([
            "tmux", "send-keys", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}",
            display
        ], check=True)
        time.sleep(0.3)
        subprocess.run([
            "tmux", "send-keys", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}",
            "", "Enter"
        ], check=True)
        return True
    except subprocess.CalledProcessError as e:
        log.error(f"Failed to inject to tmux: {e}")
        return False


def request_approval(token, admin_chat_id, new_chat_id, sender_name):
    """Notify the admin (first allowed chat) about a new chat requesting access."""
    msg = (
        f"🔔 New Telegram chat requesting access to Clawdy:\n"
        f"Name: {sender_name}\n"
        f"Chat ID: {new_chat_id}\n\n"
        f"To allow, add this chat ID to TELEGRAM_ALLOWED_CHATS in .env\n"
        f"or send: /allow {new_chat_id}"
    )
    send_message(token, admin_chat_id, msg)
    log.info(f"Sent approval request to admin for chat {new_chat_id}")


def get_updates(token, offset=None):
    params = {"timeout": 30, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        r = requests.get(url, params=params, timeout=40)
        return r.json()
    except Exception as e:
        log.error(f"getUpdates error: {e}")
        return {"ok": False, "result": []}


def main():
    log.info("Clawdy Telegram Bot starting...")
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token == "your_bot_token_here":
        log.error("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    cfg = load_config()

    # Parse allowed chats from env (overrides config)
    env_allowed = [c.strip() for c in env.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if c.strip()]
    if env_allowed:
        cfg["allowed_chats"] = list(set(cfg["allowed_chats"] + [int(c) for c in env_allowed if c.isdigit()]))
        save_config(cfg)

    # Verify bot token works
    me = tg_request(token, "getMe")
    if not me.get("ok"):
        log.error(f"Bot token invalid: {me}")
        sys.exit(1)
    bot_name = me["result"]["username"]
    log.info(f"Bot @{bot_name} connected. Allowed chats: {cfg['allowed_chats']}")

    if not cfg["allowed_chats"]:
        log.info("No allowed chats yet. Send any message to the bot to register your chat ID.")

    offset = None

    while True:
        data = get_updates(token, offset)
        if not data.get("ok"):
            time.sleep(5)
            continue

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue

            chat_id = msg["chat"]["id"]
            sender = msg["from"].get("first_name", "Unknown")
            text = msg.get("text", "")

            if not text:
                continue

            # Handle /allow command from allowed chats
            if text.startswith("/allow ") and chat_id in cfg["allowed_chats"]:
                new_id = text.split()[-1]
                if new_id.lstrip("-").isdigit():
                    cfg["allowed_chats"].append(int(new_id))
                    save_config(cfg)
                    send_message(token, chat_id, f"✅ Chat {new_id} added to allowed list.")
                continue

            # First-ever message — auto-register as the owner chat
            if not cfg["allowed_chats"]:
                log.info(f"First message from {sender} (chat {chat_id}) — registering as owner")
                cfg["allowed_chats"].append(chat_id)
                save_config(cfg)
                send_message(token, chat_id,
                    f"✅ Hi {sender}! I've registered your chat as the owner.\n"
                    f"Your Chat ID: {chat_id}\n"
                    f"Messages here will be forwarded to Clawdy."
                )
                # Also update the .env file
                env_text = ENV_FILE.read_text()
                env_text = env_text.replace("TELEGRAM_CHAT_ID=", f"TELEGRAM_CHAT_ID={chat_id}")
                env_text = env_text.replace("TELEGRAM_ALLOWED_CHATS=", f"TELEGRAM_ALLOWED_CHATS={chat_id}")
                ENV_FILE.write_text(env_text)
                continue

            # Check if chat is allowed
            if chat_id not in cfg["allowed_chats"]:
                log.warning(f"Message from unauthorized chat {chat_id} ({sender}): {text[:50]}")
                if cfg["allowed_chats"]:
                    request_approval(token, cfg["allowed_chats"][0], chat_id, sender)
                send_message(token, chat_id, "⛔ This chat is not authorized. The owner has been notified.")
                continue

            log.info(f"Message from {sender} ({chat_id}): {text[:80]}")

            # Inject into Claude tmux session
            success = inject_to_claude(text, sender, chat_id)
            if not success:
                send_message(token, chat_id, "⚠️ Failed to reach Clawdy session. Is it running?")
            else:
                start_typing(chat_id)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped.")
