#!/usr/bin/env python3
"""
Easyclaw Message Broker — persistent message store for Claude Code channels.

Singleton HTTP daemon backed by SQLite. All message sources (peer bots, cron,
system alerts) POST here. The MCP channel server polls for undelivered messages
and pushes them into the Claude session via channel notifications.

Endpoints:
  POST /send       — queue a message {source, sender, content, meta}
  POST /poll       — fetch & mark undelivered messages for a recipient
  POST /ack        — confirm message was processed (or revert to pending)
  GET  /health     — liveness check
  POST /register   — register a channel consumer (for health tracking)
  POST /heartbeat  — keep-alive from channel consumer

Port: BROKER_PORT env or 7899
DB:   ~/.easyclaw/broker.db
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s: %(message)s")
logger = logging.getLogger("broker")

EASYCLAW_DIR = Path.home() / ".easyclaw"
DB_PATH = EASYCLAW_DIR / "broker.db"
PORT = int(os.environ.get("BROKER_PORT", "7899"))

# --- Database ---

def init_db() -> sqlite3.Connection:
    """Initialize SQLite database with WAL mode."""
    EASYCLAW_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,          -- 'peer', 'cron', 'system'
            sender TEXT DEFAULT '',        -- 'karly', 'HEARTBEAT', etc
            content TEXT NOT NULL,
            meta TEXT DEFAULT '{}',        -- JSON blob for extra routing info
            status TEXT DEFAULT 'pending', -- pending, in_flight, delivered, expired
            created_at REAL NOT NULL,
            delivered_at REAL,
            ttl_seconds INTEGER DEFAULT 0  -- 0 = no expiry
        );

        CREATE TABLE IF NOT EXISTS consumers (
            id TEXT PRIMARY KEY,           -- stable identity e.g. 'superclawdy'
            pid INTEGER,
            last_seen REAL,
            registered_at REAL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
        CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
    """)

    # Expire stale messages
    now = time.time()
    conn.execute("""
        UPDATE messages SET status='expired'
        WHERE status='pending' AND ttl_seconds > 0 AND (created_at + ttl_seconds) < ?
    """, (now,))

    # Revert stuck in_flight messages (older than 60s)
    conn.execute("""
        UPDATE messages SET status='pending'
        WHERE status='in_flight' AND delivered_at < ?
    """, (now - 60,))

    logger.info("Database initialized: %s", DB_PATH)
    return conn


# --- HTTP Server ---

async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, db: sqlite3.Connection):
    """Handle a single HTTP request."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not request_line:
            writer.close()
            await writer.wait_closed()
            return

        parts = request_line.decode().strip().split(" ", 2)
        if len(parts) < 2:
            writer.close()
            await writer.wait_closed()
            return
        method, path = parts[0], parts[1]

        # Read headers
        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            line = line.decode().strip()
            if not line:
                break
            if ": " in line:
                key, val = line.split(": ", 1)
                headers[key.lower()] = val

        # Read body
        content_length = int(headers.get("content-length", "0"))
        body = b""
        if content_length > 0:
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=30)

        # Route
        if method == "GET" and path == "/health":
            response = _json_response(200, {"status": "ok", "messages_pending": _count_pending(db)})

        elif method == "POST" and path == "/send":
            data = json.loads(body.decode())
            response = _handle_send(db, data)

        elif method == "POST" and path == "/poll":
            data = json.loads(body.decode())
            response = _handle_poll(db, data)

        elif method == "POST" and path == "/ack":
            data = json.loads(body.decode())
            response = _handle_ack(db, data)

        elif method == "POST" and path == "/register":
            data = json.loads(body.decode())
            response = _handle_register(db, data)

        elif method == "POST" and path == "/heartbeat":
            data = json.loads(body.decode())
            response = _handle_heartbeat(db, data)

        elif method == "POST" and path == "/api/log-outbound":
            data = json.loads(body.decode())
            _log_chat_history(
                source=data.get("source", "clawdy"),
                sender=data.get("sender", ""),
                text=data.get("text", ""),
                ts=time.time(),
                direction="out",
            )
            response = _json_response(200, {"ok": True})

        # Legacy compatibility: accept /inject from old peer bots
        elif method == "POST" and path == "/inject":
            data = json.loads(body.decode())
            data.setdefault("source", "peer")
            data.setdefault("content", data.get("message", data.get("display", "")))
            response = _handle_send(db, data)

        else:
            response = _json_response(404, {"error": "not found"})

        writer.write(response)
        await writer.drain()
    except Exception as e:
        logger.error("Request error: %s", e)
        try:
            writer.write(_json_response(500, {"error": str(e)}))
            await writer.drain()
        except:
            pass
    finally:
        writer.close()
        await writer.wait_closed()


def _handle_send(db: sqlite3.Connection, data: dict) -> bytes:
    """Queue a new message."""
    source = data.get("source", "unknown")
    sender = data.get("sender", "")
    content = data.get("content", data.get("message", ""))
    meta = json.dumps(data.get("meta", {}))
    ttl = data.get("ttl_seconds", 0)

    if not content:
        return _json_response(400, {"error": "content required"})

    now = time.time()
    cursor = db.execute(
        "INSERT INTO messages (source, sender, content, meta, status, created_at, ttl_seconds) VALUES (?,?,?,?,?,?,?)",
        (source, sender, content, meta, "pending", now, ttl),
    )
    msg_id = cursor.lastrowid
    logger.info("Queued message #%d from %s/%s: %s", msg_id, source, sender, content[:60])

    # Also log to chat-history.jsonl for dashboard compatibility
    _log_chat_history(source, sender, content, now, direction="in")

    return _json_response(200, {"id": msg_id, "status": "queued"})


def _log_chat_history(source: str, sender: str, text: str, ts: float, direction: str = "in"):
    """Append to chat-history.jsonl in the format the dashboard expects."""
    try:
        entry = {
            "ts": ts,
            "dir": direction,
            "source": source,
            "sender": sender,
            "text": text,
        }
        chat_history = EASYCLAW_DIR / "chat-history.jsonl"
        with open(chat_history, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("Failed to log chat history: %s", e)


def _handle_poll(db: sqlite3.Connection, data: dict) -> bytes:
    """Fetch pending messages, mark as in_flight."""
    limit = data.get("limit", 10)
    now = time.time()

    # Expire old messages first
    db.execute("""
        UPDATE messages SET status='expired'
        WHERE status='pending' AND ttl_seconds > 0 AND (created_at + ttl_seconds) < ?
    """, (now,))

    rows = db.execute("""
        SELECT id, source, sender, content, meta, created_at
        FROM messages WHERE status='pending'
        ORDER BY created_at ASC LIMIT ?
    """, (limit,)).fetchall()

    messages = []
    ids = []
    for row in rows:
        messages.append({
            "id": row["id"],
            "source": row["source"],
            "sender": row["sender"],
            "content": row["content"],
            "meta": json.loads(row["meta"]),
            "created_at": row["created_at"],
        })
        ids.append(row["id"])

    if ids:
        placeholders = ",".join("?" * len(ids))
        db.execute(
            f"UPDATE messages SET status='in_flight', delivered_at=? WHERE id IN ({placeholders})",
            [now] + ids,
        )

    return _json_response(200, {"messages": messages})


def _handle_ack(db: sqlite3.Connection, data: dict) -> bytes:
    """Acknowledge message delivery."""
    msg_ids = data.get("ids", [])
    success = data.get("success", True)

    if not msg_ids:
        return _json_response(400, {"error": "ids required"})

    placeholders = ",".join("?" * len(msg_ids))
    if success:
        db.execute(f"UPDATE messages SET status='delivered' WHERE id IN ({placeholders})", msg_ids)
    else:
        # Revert to pending for retry
        db.execute(f"UPDATE messages SET status='pending', delivered_at=NULL WHERE id IN ({placeholders})", msg_ids)

    return _json_response(200, {"acked": len(msg_ids), "success": success})


def _handle_register(db: sqlite3.Connection, data: dict) -> bytes:
    """Register a channel consumer."""
    consumer_id = data.get("id", "unknown")
    pid = data.get("pid", os.getpid())
    now = time.time()

    db.execute(
        "INSERT OR REPLACE INTO consumers (id, pid, last_seen, registered_at) VALUES (?,?,?,?)",
        (consumer_id, pid, now, now),
    )
    logger.info("Consumer registered: %s (pid=%d)", consumer_id, pid)
    return _json_response(200, {"id": consumer_id})


def _handle_heartbeat(db: sqlite3.Connection, data: dict) -> bytes:
    """Update consumer heartbeat."""
    consumer_id = data.get("id", "unknown")
    db.execute("UPDATE consumers SET last_seen=? WHERE id=?", (time.time(), consumer_id))
    return _json_response(200, {"ok": True})


def _count_pending(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM messages WHERE status='pending'").fetchone()[0]


def _json_response(status: int, data: dict) -> bytes:
    body = json.dumps(data).encode()
    status_text = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(status, "OK")
    return (
        f"HTTP/1.1 {status} {status_text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"\r\n"
    ).encode() + body


# --- Main ---

async def main():
    db = init_db()

    async def handler(reader, writer):
        await handle_request(reader, writer, db)

    server = await asyncio.start_server(handler, "0.0.0.0", PORT)
    logger.info("Broker listening on port %d", PORT)

    # Periodic cleanup: expire old messages, clean delivered older than 24h
    async def cleanup_loop():
        while True:
            await asyncio.sleep(300)  # every 5 min
            now = time.time()
            db.execute("UPDATE messages SET status='expired' WHERE status='pending' AND ttl_seconds > 0 AND (created_at + ttl_seconds) < ?", (now,))
            db.execute("DELETE FROM messages WHERE status='delivered' AND delivered_at < ?", (now - 86400,))
            db.execute("DELETE FROM messages WHERE status='expired' AND created_at < ?", (now - 86400,))
            # Revert stuck in_flight (>60s)
            db.execute("UPDATE messages SET status='pending' WHERE status='in_flight' AND delivered_at < ?", (now - 60,))

    asyncio.create_task(cleanup_loop())

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
