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
import threading
import requests
import logging
from pathlib import Path
from datetime import datetime

EASYCLAW = Path.home() / ".easyclaw"
ENV_FILE = EASYCLAW / ".env"
CONFIG_FILE = EASYCLAW / "telegram-config.json"
LOG_FILE = EASYCLAW / "telegram-bot.log"
STOP_TYPING = EASYCLAW / "stop-typing"
FILES_DIR = Path.home() / "telegram-files"  # overridden in main() from env

# Rate limiting: max 5 messages per 30 seconds per chat_id
_rate_limit: dict[int, list[float]] = {}

# Typing indicator state (in-process thread)
_typing_thread: threading.Thread | None = None
_stop_typing_event = threading.Event()
_bot_token: str = ""
TMUX_SESSION = "claude"
TMUX_WINDOW = "claude"
FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB — Telegram bot download hard limit

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


def tg_request(token, method, _retries=3, **kwargs):
    url = f"https://api.telegram.org/bot{token}/{method}"
    for attempt in range(_retries):
        try:
            r = requests.post(url, json=kwargs, timeout=35)
            return r.json()
        except Exception as e:
            if attempt < _retries - 1:
                delay = 2 ** attempt  # 1s, 2s, 4s
                log.warning(f"Telegram API error ({method}), retrying in {delay}s: {e}")
                time.sleep(delay)
            else:
                log.error(f"Telegram API error ({method}) after {_retries} attempts: {e}")
    return {"ok": False}


def send_message(token, chat_id, text, **kwargs):
    return tg_request(token, "sendMessage", chat_id=chat_id, text=text, **kwargs)


def get_file_info(msg):
    """Extract (file_id, filename_hint, file_size) from a message with an attachment.
    Returns (None, None, None) if no supported file type found."""
    if "document" in msg:
        d = msg["document"]
        return d["file_id"], d.get("file_name", "document"), d.get("file_size", 0)
    if "photo" in msg:
        # Pick the largest photo size
        largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        return largest["file_id"], "photo.jpg", largest.get("file_size", 0)
    if "audio" in msg:
        a = msg["audio"]
        return a["file_id"], a.get("file_name", "audio"), a.get("file_size", 0)
    if "voice" in msg:
        v = msg["voice"]
        return v["file_id"], "voice.ogg", v.get("file_size", 0)
    if "video" in msg:
        v = msg["video"]
        return v["file_id"], v.get("file_name", "video.mp4"), v.get("file_size", 0)
    if "video_note" in msg:
        return msg["video_note"]["file_id"], "video_note.mp4", msg["video_note"].get("file_size", 0)
    if "sticker" in msg:
        s = msg["sticker"]
        ext = "webm" if s.get("is_video") else "webp"
        return s["file_id"], f"sticker.{ext}", s.get("file_size", 0)
    return None, None, None


def download_file(token, file_id, filename_hint):
    """Download a Telegram file to FILES_DIR. Returns local path or None on failure."""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    # Get file path from Telegram
    result = tg_request(token, "getFile", file_id=file_id)
    if not result.get("ok"):
        log.error(f"getFile failed: {result}")
        return None
    file_path = result["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    # Use timestamp prefix to avoid collisions
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_name = f"{timestamp}_{filename_hint}"
    local_path = FILES_DIR / local_name
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        log.info(f"Downloaded file to {local_path}")
        return local_path
    except Exception as e:
        log.error(f"File download failed: {e}")
        return None


def start_typing(chat_id, timeout=90):
    """Start background typing indicator thread. Auto-stops after timeout seconds
    even if no telegram_send is ever called (e.g. agent decides not to respond)."""
    global _typing_thread
    stop_typing()  # stop any existing thread first
    _stop_typing_event.clear()

    def _loop():
        deadline = time.time() + timeout
        while True:
            # Stop if flag written by clawdy-mcp's telegram_send
            if STOP_TYPING.exists():
                STOP_TYPING.unlink(missing_ok=True)
                break
            # Auto-stop after timeout so we don't type forever on no-reply messages
            if time.time() >= deadline:
                log.info("Typing indicator auto-stopped (timeout)")
                break
            tg_request(_bot_token, "sendChatAction", chat_id=chat_id, action="typing")
            if _stop_typing_event.wait(4):
                break

    _typing_thread = threading.Thread(target=_loop, daemon=True)
    _typing_thread.start()
    log.info(f"Typing indicator started (thread, timeout={timeout}s)")


def stop_typing():
    """Stop the typing indicator thread."""
    global _typing_thread
    if _typing_thread and _typing_thread.is_alive():
        _stop_typing_event.set()
        _typing_thread.join(timeout=2)
        log.info("Typing indicator stopped")
    _typing_thread = None

def inject_to_claude(message_text, sender_name):
    """Inject a message into the tmux Claude session."""
    display = f"[TELEGRAM from {sender_name}]: {message_text}"
    log.info(f"Injecting to Claude: {display[:80]}")
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
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=40)
            return r.json()
        except Exception as e:
            if attempt < 2:
                delay = 2 ** attempt
                log.warning(f"getUpdates error, retrying in {delay}s: {e}")
                time.sleep(delay)
            else:
                log.error(f"getUpdates error after 3 attempts: {e}")
    return {"ok": False, "result": []}


def main():
    global _bot_token
    log.info("Clawdy Telegram Bot starting...")
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    _bot_token = token

    # Set files dir from env or fall back to ~/telegram-files
    global FILES_DIR
    FILES_DIR = Path(env["TELEGRAM_FILES_DIR"]) if env.get("TELEGRAM_FILES_DIR") else Path.home() / "telegram-files"
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

            # Caption text (photos/docs can have a caption alongside the file)
            caption = msg.get("caption", "")

            if not text:
                # Check for a supported file attachment
                file_id, filename_hint, file_size = get_file_info(msg)
                if file_id and chat_id in cfg["allowed_chats"]:
                    if file_size and file_size > FILE_SIZE_LIMIT:
                        send_message(token, chat_id, f"⚠️ File too large ({file_size // (1024*1024)} MB). Max is 20 MB.")
                        continue
                    send_message(token, chat_id, f"📥 Downloading {filename_hint}...")
                    local_path = download_file(token, file_id, filename_hint)
                    if local_path:
                        text = f"[File saved: {local_path}]"
                        if caption:
                            text += f" {caption}"
                    else:
                        send_message(token, chat_id, "⚠️ Failed to download the file. Try again.")
                        continue
                elif chat_id in cfg["allowed_chats"]:
                    send_message(token, chat_id, "⚠️ Unsupported message type.")
                    continue
                else:
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

            # Rate limiting: max 5 messages per 30 seconds per chat_id
            now = time.time()
            _rate_limit.setdefault(chat_id, [])
            _rate_limit[chat_id] = [t for t in _rate_limit[chat_id] if now - t < 30]
            if len(_rate_limit[chat_id]) >= 5:
                send_message(token, chat_id, "⚠️ Slow down — I can only handle 5 messages per 30 seconds.")
                continue
            _rate_limit[chat_id].append(now)

            # Start typing BEFORE injecting so stop_typing always has a PID to kill
            start_typing(chat_id)
            success = inject_to_claude(text, sender)
            if not success:
                stop_typing()
                send_message(token, chat_id, "⚠️ Failed to reach Clawdy session. Is it running?")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped.")
