#!/usr/bin/env python3
"""
Background typing indicator loop for Telegram.
Sends 'typing' action every 4s until killed or timeout (10 min default).
Started by the bot when a message arrives, killed by clawdy-tell-telegram before replying.
Timeout prevents zombie processes if killed ungracefully.
"""
import sys
import time
import os
import requests
from pathlib import Path

ENV_FILE = Path.home() / ".easyclaw" / ".env"

def load_token():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None

def load_chat_id():
    import json
    config = Path.home() / ".easyclaw" / "telegram-config.json"
    if config.exists():
        data = json.loads(config.read_text())
        chats = data.get("allowed_chats", [])
        if chats:
            return chats[0]
    return None

def main():
    chat_id = sys.argv[1] if len(sys.argv) > 1 else load_chat_id()
    token = load_token()
    if not token or not chat_id:
        sys.exit(1)

    # Timeout in seconds (default 600s = 10 min, can override with TYPING_TIMEOUT env var)
    timeout = int(os.environ.get("TYPING_TIMEOUT", "600"))
    start_time = time.time()

    url = f"https://api.telegram.org/bot{token}/sendChatAction"
    while True:
        # Exit if timeout reached (prevents zombie processes)
        elapsed = time.time() - start_time
        if elapsed > timeout:
            break

        try:
            requests.post(url, json={"chat_id": int(chat_id), "action": "typing"}, timeout=5)
        except Exception:
            pass
        time.sleep(4)

if __name__ == "__main__":
    main()
