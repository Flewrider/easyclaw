#!/usr/bin/env python3
"""
clawdy-mcp — MCP server exposing Clawdy tools to Claude Code.

Tools exposed:
  - memory_search(query)
  - memory_add(category, title, content)
  - memory_show(id)
  - memory_list(days?)
  - telegram_send(message, end_typing?)
  - telegram_send_file(file_path, caption?)
  - activity_log(category, description)
  - set_status(status)
  - task_add(description, status?)
  - task_list()
  - task_done(pattern)
  - task_remove(pattern)
  - spawn_agent(prompt, model?, allowed_tools?)
  - converse_with_agent(session_id, prompt, model?, allowed_tools?)

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
EASYCLAW      = HOME / ".easyclaw"
MEMORY_DB     = EASYCLAW / "memories.db"
ENV_FILE      = EASYCLAW / ".env"
CONFIG_FILE   = EASYCLAW / "telegram-config.json"
ACTIVITY_LOG  = EASYCLAW / "activity-log.md"
STATUS_FILE   = EASYCLAW / "status"
TASKS_FILE    = EASYCLAW / "tasks.md"
AGENT_LOG     = EASYCLAW / "agent-sessions.jsonl"

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
    import subprocess

    # Kill any stale typing loop processes before sending message
    try:
        subprocess.run(["pkill", "-f", "clawdy-typing-loop"], timeout=2)
    except Exception:
        pass

    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token == "your_bot_token_here":
        return "TELEGRAM_BOT_TOKEN not configured."
    chat_id = get_telegram_chat_id()
    if not chat_id:
        return "No Telegram chat ID configured. Send a message to the bot first."

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


def impl_telegram_send_file(file_path: str, caption: str | None = None) -> str:
    import requests  # local import — only needed if telegram is used
    path = Path(file_path)
    if not path.exists():
        return f"File not found: {file_path}"
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token == "your_bot_token_here":
        return "TELEGRAM_BOT_TOKEN not configured."
    chat_id = get_telegram_chat_id()
    if not chat_id:
        return "No Telegram chat ID configured. Send a message to the bot first."
    try:
        with open(path, "rb") as f:
            data: dict[str, Any] = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "Markdown"
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data=data,
                files={"document": (path.name, f)},
                timeout=60,
            )
        result = r.json()
        if result.get("ok"):
            return f"File sent: {path.name} ({path.stat().st_size} bytes)"
        return f"Failed to send file: {result}"
    except Exception as e:
        return f"Error sending file: {e}"


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


def _init_tasks_file() -> None:
    """Create tasks.md with default structure if it doesn't exist."""
    if not TASKS_FILE.exists():
        TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TASKS_FILE.write_text(
            "# Clawdy Task List\n"
            "*Persistent task tracking — survives restarts*\n\n"
            "## Pending\n\n"
            "## In Progress\n\n"
            "## Done (recent)\n"
        )


def impl_task_add(description: str, status: str = "pending") -> str:
    _init_tasks_file()
    today = datetime.now().strftime("%Y-%m-%d")
    section_map = {
        "pending":     ("## Pending",     "- [ ]"),
        "in_progress": ("## In Progress", "- [~]"),
    }
    if status not in section_map:
        return f"Invalid status '{status}'. Use 'pending' or 'in_progress'."
    section_header, checkbox = section_map[status]
    entry = f"{checkbox} [{today}] {description}\n"

    lines = TASKS_FILE.read_text().splitlines(keepends=True)
    insert_at = None
    for i, line in enumerate(lines):
        if line.strip() == section_header:
            insert_at = i + 1
            break
    if insert_at is None:
        return f"Section '{section_header}' not found in tasks.md."

    lines.insert(insert_at, entry)
    TASKS_FILE.write_text("".join(lines))
    return f"Task added ({status}): {description}"


def impl_task_list() -> str:
    if not TASKS_FILE.exists():
        return "No tasks file found."
    lines = TASKS_FILE.read_text().splitlines()
    tasks = []
    current_section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            current_section = stripped[3:]
        elif stripped.startswith("- ["):
            tasks.append(f"[{current_section}] {stripped}")
    if not tasks:
        return "No tasks found."
    return "\n".join(tasks)


def impl_task_done(pattern: str) -> str:
    """Mark the first task matching `pattern` as done (moves to Done section)."""
    _init_tasks_file()
    today = datetime.now().strftime("%Y-%m-%d")
    lines = TASKS_FILE.read_text().splitlines(keepends=True)

    # Find the matching task line (pending or in-progress)
    match_idx = None
    matched_line = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (stripped.startswith("- [ ]") or stripped.startswith("- [~]")) and pattern.lower() in stripped.lower():
            match_idx = i
            matched_line = stripped
            break

    if match_idx is None:
        return f"No pending/in-progress task found matching: '{pattern}'"

    # Extract description (strip checkbox + date prefix)
    desc = matched_line
    for prefix in ("- [ ] ", "- [~] "):
        if desc.startswith(prefix):
            desc = desc[len(prefix):]
            break
    done_entry = f"- [x] [{today}] {desc}\n"

    # Remove the original line
    lines.pop(match_idx)

    # Find Done section and append there
    for i, line in enumerate(lines):
        if line.strip() == "## Done (recent)":
            lines.insert(i + 1, done_entry)
            break
    else:
        lines.append(f"\n## Done (recent)\n{done_entry}")

    TASKS_FILE.write_text("".join(lines))
    return f"Task marked done: {desc}"


def impl_task_remove(pattern: str) -> str:
    """Remove a task entirely (any status) matching `pattern`."""
    _init_tasks_file()
    lines = TASKS_FILE.read_text().splitlines(keepends=True)
    new_lines = []
    removed = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- [") and pattern.lower() in stripped.lower():
            removed.append(stripped)
        else:
            new_lines.append(line)
    if not removed:
        return f"No task found matching: '{pattern}'"
    TASKS_FILE.write_text("".join(new_lines))
    return f"Removed {len(removed)} task(s):\n" + "\n".join(removed)


def _log_agent_session(
    entry_type: str,  # "spawn" | "converse"
    prompt: str,
    result_json: str,
    model: str,
    session_id: str | None = None,
) -> None:
    """Append a JSONL entry to agent-sessions.jsonl for long-term lookup."""
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        data = {"result": result_json}
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "type": entry_type,
        "session_id": data.get("session_id") or session_id or "",
        "model": model,
        "prompt": prompt,
        "result": data.get("result", ""),
        "is_error": data.get("is_error", False),
        "cost_usd": data.get("cost_usd", 0),
        "duration_ms": data.get("duration_ms", 0),
        "num_turns": data.get("num_turns", 0),
    }
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


# Tools that subagents are never allowed to use regardless of caller's allowed_tools
_AGENT_BLOCKED_TOOLS = ["mcp__clawdy-mcp__telegram_send"]


def _build_agent_cmd(
    prompt: str,
    model: str,
    allowed_tools: list[str] | None,
    session_id: str | None = None,
) -> list[str]:
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    if allowed_tools is not None:
        # Explicit allowlist — strip any blocked tools and pass as allowedTools
        safe = [t for t in allowed_tools if t not in _AGENT_BLOCKED_TOOLS]
        if safe:
            cmd += ["--allowedTools"] + safe
        else:
            cmd += ["--tools", ""]  # no tools at all
    else:
        # Default: full permissions but block telegram
        cmd += ["--dangerously-skip-permissions"]
        cmd += ["--disallowedTools", ",".join(_AGENT_BLOCKED_TOOLS)]
    return cmd


def _parse_agent_output(raw: str) -> str:
    """Parse JSON output from claude --output-format json into a clean summary."""
    data = json.loads(raw)
    clean = {
        "result": data.get("result", ""),
        "session_id": data.get("session_id", ""),
        "is_error": data.get("is_error", False),
        "cost_usd": round(data.get("total_cost_usd", 0), 6),
        "duration_ms": data.get("duration_ms", 0),
        "num_turns": data.get("num_turns", 0),
    }
    return json.dumps(clean, indent=2)


async def impl_spawn_agent(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    allowed_tools: list[str] | None = None,
) -> str:
    """Launch a headless Claude subagent and return its response + session ID."""
    cmd = _build_agent_cmd(prompt, model, allowed_tools)
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # allow nested claude session
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "Agent timed out after 300 seconds."
    except Exception as e:
        return f"Error launching agent: {e}"

    if proc.returncode != 0:
        return f"Agent failed (exit {proc.returncode}): {stderr.decode()[:500]}"

    try:
        parsed = _parse_agent_output(stdout.decode())
        _log_agent_session("spawn", prompt, parsed, model)
        return parsed
    except (json.JSONDecodeError, KeyError) as e:
        return f"Failed to parse agent output: {e}\nRaw: {stdout.decode()[:500]}"


async def impl_converse_with_agent(
    session_id: str,
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    allowed_tools: list[str] | None = None,
) -> str:
    """Send a follow-up prompt to a previously spawned headless agent session."""
    cmd = _build_agent_cmd(prompt, model, allowed_tools, session_id=session_id)
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "Agent timed out after 300 seconds."
    except Exception as e:
        return f"Error conversing with agent: {e}"

    if proc.returncode != 0:
        return f"Agent failed (exit {proc.returncode}): {stderr.decode()[:500]}"

    try:
        parsed = _parse_agent_output(stdout.decode())
        _log_agent_session("converse", prompt, parsed, model, session_id=session_id)
        return parsed
    except (json.JSONDecodeError, KeyError) as e:
        return f"Failed to parse agent output: {e}\nRaw: {stdout.decode()[:500]}"


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
            description="Send a message to the user via Telegram. By default the typing indicator keeps running; set end_typing=true on the final message to stop it.",
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
            name="telegram_send_file",
            description="Send a file to the user via Telegram's sendDocument API.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file on disk",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption (Markdown supported)",
                    },
                },
                "required": ["file_path"],
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
            description="Set working status to 'busy' (suppresses cron interruptions) or 'idle' (allows cron checks). Busy auto-clears after 2 hours.",
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
        types.Tool(
            name="task_add",
            description="Add a task to the persistent task list (read by cron every 30 min).",
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Task description"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress"],
                        "description": "Initial status (default: pending)",
                        "default": "pending",
                    },
                },
                "required": ["description"],
            },
        ),
        types.Tool(
            name="task_list",
            description="List all tasks in the task list (all statuses).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="task_done",
            description="Mark a task as done. Matches by partial description text.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Partial text to match the task"},
                },
                "required": ["pattern"],
            },
        ),
        types.Tool(
            name="task_remove",
            description="Remove a task entirely from the list. Matches by partial description text.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Partial text to match the task"},
                },
                "required": ["pattern"],
            },
        ),
        types.Tool(
            name="spawn_agent",
            description=(
                "Launch a headless Claude Code subagent with a prompt. Returns the agent's response, "
                "session_id (use with converse_with_agent for follow-ups), cost, and duration. "
                "telegram_send is always blocked for subagents. Default model: haiku."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The prompt to send to the subagent"},
                    "model": {
                        "type": "string",
                        "description": "Model to use. Aliases: 'haiku', 'sonnet', 'opus', or full model ID. Default: claude-haiku-4-5-20251001",
                        "default": "claude-haiku-4-5-20251001",
                    },
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Explicit list of tools to allow (e.g. ['Bash', 'Read', 'Write']). "
                            "Omit to grant all permissions (--dangerously-skip-permissions). "
                            "telegram_send is always blocked regardless."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        ),
        types.Tool(
            name="converse_with_agent",
            description=(
                "Send a follow-up prompt to a previously spawned headless agent session. "
                "Use the session_id returned by spawn_agent. Returns the agent's response "
                "and the same session_id for further turns."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID returned by spawn_agent",
                    },
                    "prompt": {"type": "string", "description": "Follow-up prompt to send"},
                    "model": {
                        "type": "string",
                        "description": "Model to use (defaults to claude-haiku-4-5-20251001)",
                        "default": "claude-haiku-4-5-20251001",
                    },
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Same as spawn_agent. Omit for full permissions (minus telegram).",
                    },
                },
                "required": ["session_id", "prompt"],
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
        elif name == "telegram_send_file":
            result = impl_telegram_send_file(
                arguments["file_path"], arguments.get("caption")
            )
        elif name == "activity_log":
            result = impl_activity_log(arguments["category"], arguments["description"])
        elif name == "set_status":
            result = impl_set_status(arguments["status"])
        elif name == "task_add":
            result = impl_task_add(arguments["description"], arguments.get("status", "pending"))
        elif name == "task_list":
            result = impl_task_list()
        elif name == "task_done":
            result = impl_task_done(arguments["pattern"])
        elif name == "task_remove":
            result = impl_task_remove(arguments["pattern"])
        elif name == "spawn_agent":
            result = await impl_spawn_agent(
                arguments["prompt"],
                arguments.get("model", "claude-haiku-4-5-20251001"),
                arguments.get("allowed_tools"),
            )
        elif name == "converse_with_agent":
            result = await impl_converse_with_agent(
                arguments["session_id"],
                arguments["prompt"],
                arguments.get("model", "claude-haiku-4-5-20251001"),
                arguments.get("allowed_tools"),
            )
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
