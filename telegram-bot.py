#!/usr/bin/env python3
"""
EasyClaw Telegram Bot Bridge
Polls Telegram for messages and injects them into Claude Code session.

This is a placeholder. Copy your actual telegram_bot.py here, or use:
  python3 -m pip install python-telegram-bot requests

Environment variables (from .env):
  TELEGRAM_BOT_TOKEN — your bot token from BotFather
  TELEGRAM_CHAT_ID — auto-registered on first message
"""

import os
import sys

print("EasyClaw Telegram Bot Bridge")
print(f"Token: {os.environ.get('TELEGRAM_BOT_TOKEN', 'NOT SET')[:10]}...")
print("Note: This is a placeholder. Implement the actual bot logic here.")
print("Keep this process running to bridge Telegram ↔ Claude Code.")

# Keep running
try:
    while True:
        import time
        time.sleep(60)
except KeyboardInterrupt:
    print("Bot stopped.")
    sys.exit(0)
