#!/usr/bin/env python3
"""
Easyclaw Bridge Channel — MCP channel server for Claude Code.

Polls the broker for pending messages and pushes them into the Claude session
via MCP channel notifications. Exposes send_to_peer, set_status, and
activity_log tools.

Loaded via: --dangerously-load-development-channels server:easyclaw-bridge
Requires broker.py running on BROKER_PORT (default 7899).
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp import types

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s: %(message)s")
logger = logging.getLogger("easyclaw-bridge")

EASYCLAW_DIR = Path.home() / ".easyclaw"
BROKER_PORT = int(os.environ.get("BROKER_PORT", "7899"))
BROKER_URL = f"http://127.0.0.1:{BROKER_PORT}"
POLL_INTERVAL = 1.0  # seconds
HEARTBEAT_INTERVAL = 15.0  # seconds
CONSUMER_ID = "superclawdy"

# Load identity
identity_file = EASYCLAW_DIR / "identity"
if identity_file.exists():
    CONSUMER_ID = identity_file.read_text().strip().lower().replace(" ", "")

# Load peers config
PEERS_FILE = EASYCLAW_DIR / "peers.json"


def load_peers() -> dict:
    if PEERS_FILE.exists():
        return json.loads(PEERS_FILE.read_text())
    return {}


# --- MCP Server ---

server = Server("easyclaw-bridge")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="send_to_peer",
            description="Send a message to a peer Claude instance over the Tailscale bridge.",
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message to send"},
                    "recipient": {"type": "string", "description": "Peer name (e.g. 'karly')"},
                },
                "required": ["message", "recipient"],
            },
        ),
        types.Tool(
            name="set_status",
            description="Set busy/idle status. Busy suppresses heartbeat cron injections.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["busy", "idle"], "description": "Status to set"},
                },
                "required": ["status"],
            },
        ),
        types.Tool(
            name="activity_log",
            description="Log activity to ~/.easyclaw/activity-log.md",
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Category (projects, bugs, ideas, learning, tasks, system)"},
                    "description": {"type": "string", "description": "Activity description"},
                },
                "required": ["category", "description"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    if name == "send_to_peer":
        recipient = arguments["recipient"].lower()
        message = arguments["message"]
        peers = load_peers()

        if recipient not in peers:
            return [types.TextContent(type="text", text=f"Unknown peer: {recipient}. Available: {', '.join(peers.keys())}")]

        raw = peers[recipient]
        # Support both formats: "ip_string" or {ip, port, broker_port, api_key}
        if isinstance(raw, str):
            peer_info = {"ip": raw}
        else:
            peer_info = raw
        peer_ip = peer_info["ip"]
        peer_port = peer_info.get("broker_port", peer_info.get("port", 7899))
        peer_url = f"http://{peer_ip}:{peer_port}/send"

        payload = {
            "source": "peer",
            "sender": CONSUMER_ID,
            "content": message,
        }

        try:
            async with aiohttp.ClientSession() as session:
                headers = {}
                if peer_info.get("api_key"):
                    headers["X-API-Key"] = peer_info["api_key"]
                async with session.post(peer_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return [types.TextContent(type="text", text=f"Sent to peer: {message[:80]}")]
                    else:
                        # Fallback: try legacy /inject endpoint
                        legacy_port = peer_info.get("port", 8766)
                        legacy_url = f"http://{peer_ip}:{legacy_port}/inject"
                        async with session.post(legacy_url, json={"message": message, "sender": CONSUMER_ID, "source": "peer"}, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                            if resp2.status == 200:
                                return [types.TextContent(type="text", text=f"Sent to peer (legacy): {message[:80]}")]
                            return [types.TextContent(type="text", text=f"Peer returned {resp.status}/{resp2.status}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Failed to send to peer: {e}")]

    elif name == "set_status":
        status = arguments["status"]
        (EASYCLAW_DIR / "status").write_text(status)
        return [types.TextContent(type="text", text=f"Status set to: {status}")]

    elif name == "activity_log":
        category = arguments["category"]
        description = arguments["description"]
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        log_file = EASYCLAW_DIR / "activity-log.md"
        with open(log_file, "a") as f:
            f.write(f"[{timestamp}] {category}: {description}\n")
        return [types.TextContent(type="text", text=f"Logged: [{timestamp}] {category}: {description}")]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# --- Broker Communication ---

async def broker_request(path: str, data: dict = None) -> dict:
    """Make a request to the broker."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{BROKER_URL}{path}"
            if data is not None:
                async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return await resp.json()
            else:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return await resp.json()
    except Exception as e:
        logger.debug("Broker request failed: %s", e)
        return {}


async def ensure_broker():
    """Start broker if not running."""
    try:
        result = await broker_request("/health")
        if result.get("status") == "ok":
            logger.info("Broker already running (pending: %d)", result.get("messages_pending", 0))
            return
    except:
        pass

    logger.info("Starting broker daemon...")
    broker_path = Path(__file__).parent / "broker.py"
    log_path = EASYCLAW_DIR / "logs" / "broker.log"
    log_file = open(str(log_path), "a")
    try:
        subprocess.Popen(
            [sys.executable, str(broker_path)],
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
        )
    except Exception:
        log_file.close()
        raise
    # Wait for broker to start
    for _ in range(10):
        await asyncio.sleep(0.5)
        try:
            result = await broker_request("/health")
            if result.get("status") == "ok":
                logger.info("Broker started successfully")
                return
        except:
            pass
    logger.warning("Broker may not have started — continuing anyway")


# --- Channel Notification ---

def build_channel_notification(content: str, meta: dict[str, str] | None = None) -> SessionMessage:
    """Build a raw JSON-RPC notification for notifications/claude/channel.

    Claude Code expects:
      method: "notifications/claude/channel"
      params: { content: string, meta?: Record<string, string> }
    """
    params = {"content": content}
    if meta:
        params["meta"] = meta

    jsonrpc_notification = types.JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params=params,
    )
    return SessionMessage(message=types.JSONRPCMessage(jsonrpc_notification))


# --- Polling + Push Loop ---

async def poll_and_push(write_stream):
    """Poll broker for messages and push to Claude via channel notifications."""
    # Wait for MCP initialization handshake to complete before sending notifications.
    # The client must send initialize + initialized before it accepts notifications.
    await asyncio.sleep(3)
    while True:
        try:
            result = await broker_request("/poll", {"limit": 10})
            messages = result.get("messages", [])

            ack_ids = []
            for msg in messages:
                try:
                    # Build meta — all values must be strings (Record<string, string>)
                    meta = {}
                    raw_meta = msg.get("meta", {})
                    if isinstance(raw_meta, dict):
                        for k, v in raw_meta.items():
                            meta[str(k)] = str(v)
                    meta["source"] = msg["source"]
                    meta["sender"] = msg.get("sender", "")
                    meta["msg_id"] = str(msg["id"])

                    # Format content with sender/source prefix so Claude sees who sent it
                    content = msg["content"]
                    source = msg["source"]
                    sender = msg.get("sender", "")
                    if source == "peer" and sender:
                        content = f"[PEER from {sender}]: {content}"
                    elif source == "cron" and sender:
                        content = f"[CRON - {sender}] {content}"
                    elif source == "cron":
                        content = f"[CRON] {content}"
                    elif source == "dashboard" and sender:
                        content = f"[DASHBOARD from {sender}]: {content}"

                    notification = build_channel_notification(content, meta)
                    await write_stream.send(notification)

                    ack_ids.append(msg["id"])
                    logger.info("Pushed message #%d (%s/%s) to Claude",
                                msg["id"], msg["source"], msg.get("sender", ""))
                except Exception as e:
                    logger.error("Failed to push message #%d: %s", msg["id"], e)

            # Acknowledge delivered messages
            if ack_ids:
                await broker_request("/ack", {"ids": ack_ids, "success": True})

        except Exception as e:
            logger.debug("Poll error: %s", e)

        await asyncio.sleep(POLL_INTERVAL)


async def heartbeat_loop():
    """Send periodic heartbeats to broker."""
    while True:
        await broker_request("/heartbeat", {"id": CONSUMER_ID})
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# --- Main ---

async def run():
    # Ensure log directory exists
    (EASYCLAW_DIR / "logs").mkdir(parents=True, exist_ok=True)

    # Start broker if needed
    await ensure_broker()

    # Register with broker
    await broker_request("/register", {"id": CONSUMER_ID, "pid": os.getpid()})

    # Start MCP server on stdio
    async with stdio_server() as (read_stream, write_stream):
        # Declare claude/channel capability so Claude Code registers this server
        init_options = server.create_initialization_options(
            experimental_capabilities={"claude/channel": {}},
        )

        # Start background tasks — write_stream is now available for notifications
        poll_task = asyncio.create_task(poll_and_push(write_stream))
        heartbeat_task = asyncio.create_task(heartbeat_loop())

        try:
            # Run MCP server (this blocks until session ends)
            await server.run(read_stream, write_stream, init_options)
        finally:
            poll_task.cancel()
            heartbeat_task.cancel()


if __name__ == "__main__":
    asyncio.run(run())
