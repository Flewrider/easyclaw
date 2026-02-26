#!/usr/bin/env python3
"""
clawdy-mcp — MCP server exposing Clawdy tools to Claude Code.

Tools exposed:
  - memory_search(query)
  - memory_add(category, title, content)
  - memory_show(id)
  - memory_list(days?)
  - telegram_send(message, end_typing?)
  - activity_log(category, description)
  - set_status(status)

Run via stdio — registered in ~/.claude/settings.json.
Install: pip install mcp
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.lowlevel import NotificationOptions

# ── Paths ────────────────────────────────────────────────────────────────────

HOME = Path.home()
MEMORY_DB   = HOME / ".claude" / "memory" / "memories.db"
ENV_FILE    = HOME / ".claude" / "memory" / ".env"
CONFIG_FILE = HOME / ".claude" / "memory" / "telegram-config.json"
TYPING_PID  = HOME / ".claude" / "memory" / "telegram-typing.pid"
TYPING_LOOP = HOME / ".claude" / "memory" / "clawdy-typing-loop.py"
ACTIVITY_LOG = HOME / ".claude" / "memory" / "activity-log.md"
STATUS_FILE = HOME / ".claude" / "memory" / "status"

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def get_telegram_chat_id() -> int | None:
    env = load_env()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if chat_id and chat_id.lstrip("-").isdigit():
        return int(chat_id)
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text())
        chats = cfg.get("allowed_chats", [])
        if chats:
            return chats[0]
    return None


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(MEMORY_DB)
    conn.row_factory = sqlite3.Row
    return conn


def stop_typing() -> None:
    if TYPING_PID.exists():
        try:
            pid = int(TYPING_PID.read_text().strip())
            os.kill(pid, 9)
        except (ProcessLookupError, ValueError):
            pass
        TYPING_PID.unlink(missing_ok=True)


def start_typing(chat_id: int) -> None:
    stop_typing()
    proc = subprocess.Popen(
        [sys.executable, str(TYPING_LOOP), str(chat_id)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    TYPING_PID.write_text(str(proc.pid))


# ── Tool implementations ──────────────────────────────────────────────────────

def impl_memory_search(query: str) -> str:
    if not MEMORY_DB.exists():
        return "Memory database not found."
    with db_connect() as conn:
        try:
            rows = conn.execute(
                """SELECT m.id, m.category, m.title, m.importance,
                          snippet(memories_fts, 2, '**', '**', '…', 20) AS snippet
                   FROM memories_fts f
                   JOIN memories m ON m.id = f.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY rank
                   LIMIT 10""",
                (query,),
            ).fetchall()
        except sqlite3.OperationalError:
            # Fallback: LIKE search if FTS not available
            rows = conn.execute(
                """SELECT id, category, title, importance, content AS snippet
                   FROM memories
                   WHERE title LIKE ? OR content LIKE ?
                   LIMIT 10""",
                (f"%{query}%", f"%{query}%"),
            ).fetchall()
    if not rows:
        return f"No memories found matching '{query}'."
    lines = [f"Found {len(rows)} result(s) for '{query}':\n"]
    for r in rows:
        lines.append(f"[{r['id']}] ({r['category']}) {r['title']} (importance: {r['importance']})")
        lines.append(f"  {r['snippet']}\n")
    return "\n".join(lines)


def impl_memory_add(category: str, title: str, content: str) -> str:
    if not MEMORY_DB.exists():
        return "Memory database not found."
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO memories (category, title, content, importance, created_at, updated_at) "
            "VALUES (?, ?, ?, 5, datetime('now'), datetime('now'))",
            (category, title, content),
        )
        row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    # Rebuild MEMORY.md
    rebuild_script = HOME / ".claude" / "memory" / "clawdy-memory.py"
    if rebuild_script.exists():
        subprocess.run([sys.executable, str(rebuild_script), "rebuild-md"],
                       capture_output=True)
    return f"Memory saved (id: {row_id}): [{category}] {title}"


def impl_memory_show(memory_id: int) -> str:
    if not MEMORY_DB.exists():
        return "Memory database not found."
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id, category, title, content, importance, tags, created_at, updated_at "
            "FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
    if not row:
        return f"No memory found with id {memory_id}."
    return (
        f"[{row['id']}] {row['title']}\n"
        f"Category: {row['category']} | Importance: {row['importance']}\n"
        f"Tags: {row['tags'] or 'none'}\n"
        f"Created: {row['created_at']} | Updated: {row['updated_at']}\n\n"
        f"{row['content']}"
    )


def impl_memory_list(days: int = 7) -> str:
    if not MEMORY_DB.exists():
        return "Memory database not found."
    since = (datetime.now() - timedelta(days=days)).isoformat()
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, category, title, importance, updated_at FROM memories "
            "WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 30",
            (since,),
        ).fetchall()
    if not rows:
        return f"No memories updated in the last {days} days."
    lines = [f"Memories updated in last {days} days ({len(rows)}):\n"]
    for r in rows:
        lines.append(f"[{r['id']}] ({r['category']}) {r['title']}  — {r['updated_at'][:10]}")
    return "\n".join(lines)


def impl_telegram_send(message: str, end_typing: bool = False) -> str:
    import requests  # local import — only needed if telegram is used
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token == "your_bot_token_here":
        return "TELEGRAM_BOT_TOKEN not configured."
    chat_id = get_telegram_chat_id()
    if not chat_id:
        return "No Telegram chat ID configured. Send a message to the bot first."

    if end_typing:
        stop_typing()

    MAX_LEN = 4096
    chunks = [message[i:i+MAX_LEN] for i in range(0, len(message), MAX_LEN)]
    result = {}
    for chunk in chunks:
        # Try Markdown, fallback to plain
        for parse_mode in ["Markdown", None]:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json=payload, timeout=15,
                )
                result = r.json()
                if result.get("ok"):
                    break
            except Exception as e:
                return f"Request failed: {e}"
        else:
            return f"Failed to send chunk: {result}"

    sent_info = f"{len(message)} chars" if len(chunks) == 1 else f"{len(message)} chars in {len(chunks)} parts"
    return f"Sent ({sent_info}). Typing indicator: {'stopped' if end_typing else 'still running'}."


def impl_activity_log(category: str, description: str) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {category}: {description}\n"
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(ACTIVITY_LOG, "a") as f:
        f.write(entry)
    return f"Logged: {entry.strip()}"


def impl_set_status(status: str) -> str:
    if status not in ("busy", "idle"):
        return f"Invalid status '{status}'. Use 'busy' or 'idle'."
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(status)
    return f"Status set to: {status}"


# ── MCP Server ────────────────────────────────────────────────────────────────

server = Server("clawdy-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="memory_search",
            description="Full-text search across all saved memories. Returns matching titles, snippets, and IDs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="memory_add",
            description="Save a new memory to the persistent memory database.",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Category: system, user_preferences, tools, projects, bugs, ideas",
                    },
                    "title": {"type": "string", "description": "Short descriptive title"},
                    "content": {"type": "string", "description": "Full memory content"},
                },
                "required": ["category", "title", "content"],
            },
        ),
        types.Tool(
            name="memory_show",
            description="Retrieve full content of a memory by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Memory ID from memory_search or memory_list"},
                },
                "required": ["id"],
            },
        ),
        types.Tool(
            name="memory_list",
            description="List recently updated memories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "How many days back to look (default: 7)",
                        "default": 7,
                    },
                },
            },
        ),
        types.Tool(
            name="telegram_send",
            description=(
                "Send a message to the user via Telegram. "
                "By default the typing indicator keeps running (for multi-part replies). "
                "Set end_typing=true on the final message to stop the indicator."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message text (Markdown supported)"},
                    "end_typing": {
                        "type": "boolean",
                        "description": "Stop the typing indicator after sending (use on final message)",
                        "default": False,
                    },
                },
                "required": ["message"],
            },
        ),
        types.Tool(
            name="activity_log",
            description="Log an activity to the activity log (appears in optional daily briefings).",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Category: projects, bugs, ideas, learning, tasks, system",
                    },
                    "description": {"type": "string", "description": "What was done"},
                },
                "required": ["category", "description"],
            },
        ),
        types.Tool(
            name="set_status",
            description=(
                "Set working status to 'busy' (suppresses cron interruptions) "
                "or 'idle' (allows cron checks). Busy auto-clears after 2 hours."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["busy", "idle"],
                        "description": "New status",
                    },
                },
                "required": ["status"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        if name == "memory_search":
            result = impl_memory_search(arguments["query"])
        elif name == "memory_add":
            result = impl_memory_add(
                arguments["category"], arguments["title"], arguments["content"]
            )
        elif name == "memory_show":
            result = impl_memory_show(int(arguments["id"]))
        elif name == "memory_list":
            result = impl_memory_list(int(arguments.get("days", 7)))
        elif name == "telegram_send":
            result = impl_telegram_send(
                arguments["message"], bool(arguments.get("end_typing", False))
            )
        elif name == "activity_log":
            result = impl_activity_log(arguments["category"], arguments["description"])
        elif name == "set_status":
            result = impl_set_status(arguments["status"])
        else:
            result = f"Unknown tool: {name}"
    except Exception as e:
        result = f"Error in {name}: {e}"

    return [types.TextContent(type="text", text=result)]


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="clawdy-mcp",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
