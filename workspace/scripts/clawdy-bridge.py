#!/usr/bin/env python3
"""
Clawdy Telegram Bot Bridge + Management Dashboard
- Polls Telegram for new messages from authorized chats
- Injects them into the tmux Claude session via a serialized queue
  (prevents race conditions when Telegram + peer messages arrive simultaneously)
- Serves a management dashboard via HTTP (Tailscale-only, DASHBOARD_PORT)
- Run as a systemd service: clawdy-bridge.service
"""

import os
import re
import sys
import json
import time
import queue as _queue_module
import subprocess
import threading
import requests
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime

EASYCLAW = Path.home() / ".easyclaw"
ENV_FILE = EASYCLAW / ".env"
CONFIG_FILE = EASYCLAW / "telegram-config.json"
LOG_FILE = EASYCLAW / "clawdy-bridge.log"
STOP_TYPING = EASYCLAW / "stop-typing"
STATUS_FILE = EASYCLAW / "status"
ACTIVITY_LOG = EASYCLAW / "activity-log.md"
CHAT_HISTORY = EASYCLAW / "chat-history.jsonl"
PENDING_QUEUE = EASYCLAW / "pending-injections.jsonl"  # persisted queue across restarts
FILES_DIR = Path.home() / "telegram-files"  # overridden in main() from env

# Whisper model (loaded once on first voice message, then cached)
_whisper_model = None

# Injection queue — all tmux send-keys calls go through this
_inject_queue = _queue_module.Queue()

# Typing indicator state
_typing_thread: threading.Thread | None = None
_stop_typing_event = threading.Event()
_bot_token: str = ""
TMUX_SESSION = "claude"
TMUX_WINDOW = "claude"
FILE_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB

# Rate limiting: max 5 messages per 30 seconds per chat_id
_rate_limit: dict[int, list[float]] = {}

# Restart context: set by MCP before planned restart; None = no planned restart pending
_restart_pending: str | None = None  # the resume context message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clawdy</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f0f0f;color:#e8e8e8;font-family:-apple-system,'Segoe UI',system-ui,sans-serif;height:100dvh;overflow:hidden}
#app{display:flex;flex-direction:column;height:100dvh}

/* Header */
#header{background:#1a1a1a;border-bottom:1px solid #2a2a2a;padding:10px 16px;display:flex;align-items:center;gap:10px;flex-shrink:0}
#header h1{font-size:16px;font-weight:700;color:#fff;letter-spacing:.2px}
.dot{width:8px;height:8px;border-radius:50%;background:#4cd964;flex-shrink:0;transition:background .4s;box-shadow:0 0 6px #4cd96480}
.dot.busy{background:#ff9500;box-shadow:0 0 6px #ff950080}
.dot.offline{background:#ff3b30;box-shadow:0 0 6px #ff3b3080}
.dot.unknown{background:#636366;box-shadow:none}
#status-text{font-size:12px;color:#888}
#queue-badge{background:#2a2a2a;border-radius:10px;padding:2px 8px;font-size:11px;color:#ff9500;display:none;margin-left:4px}

/* Tabs */
#tabs{display:flex;background:#1a1a1a;border-bottom:1px solid #2a2a2a;flex-shrink:0;overflow-x:auto}
.tab{padding:9px 18px;font-size:13px;font-weight:500;color:#888;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap;transition:color .2s}
.tab:hover{color:#ccc}
.tab.active{color:#fff;border-bottom-color:#007aff}
#ts-label{margin-left:auto;font-size:11px;color:#444;padding-right:4px;white-space:nowrap}

/* Tab panels */
.panel{display:none;flex:1;flex-direction:column;min-height:0;overflow:hidden}
.panel.active{display:flex}

/* ── CHAT TAB ── */
#chat-log{flex:1;overflow-y:auto;padding:12px 12px 4px;display:flex;flex-direction:column;gap:6px}
#chat-log::-webkit-scrollbar{width:4px}
#chat-log::-webkit-scrollbar-thumb{background:#333;border-radius:2px}
#load-more{align-self:center;background:transparent;border:1px solid #333;border-radius:12px;padding:4px 14px;color:#666;font-size:11px;cursor:pointer;margin-bottom:2px;display:none}
#load-more:hover{color:#aaa;border-color:#555}

/* Message bubbles */
.bubble-wrap{display:flex;flex-direction:column;max-width:82%;gap:2px}
.bubble-wrap.user{align-self:flex-end;align-items:flex-end}
.bubble-wrap.left{align-self:flex-start;align-items:flex-start}
.bubble-meta{display:flex;align-items:center;gap:5px;padding:0 4px}
.bubble-sender{font-size:11px;font-weight:600;color:#888}
.bubble-src{font-size:10px;border-radius:4px;padding:1px 5px;font-weight:700;letter-spacing:.3px}
.bubble{padding:8px 12px;border-radius:18px;font-size:14px;line-height:1.45;word-break:break-word;white-space:pre-wrap;position:relative}
.bubble-time{font-size:10px;color:#555;padding:0 4px}
/* User messages (Ben) — right side, blue */
.bubble-wrap.user .bubble{background:#007aff;border-bottom-right-radius:5px;color:#fff}
.bubble-wrap.user .bubble-time{text-align:right}
/* Clawdy responses — left side, dark green */
.bubble-wrap.clawdy .bubble{background:#1a3a1a;border-bottom-left-radius:5px;color:#e8e8e8;border-left:2px solid #4cd964}
.bubble-wrap.clawdy .bubble-sender{color:#4cd964}
/* Peer — left, purple */
.bubble-wrap.src-peer .bubble{background:#1e1228;border-bottom-left-radius:5px;color:#e8e8e8;border-left:2px solid #af52de}
.bubble-wrap.src-peer .bubble-sender{color:#af52de}
.bubble-wrap.src-peer .bubble-src{background:#2d1a40;color:#af52de}
/* Cron — left, orange */
.bubble-wrap.src-cron .bubble{background:#1e1400;border-bottom-left-radius:5px;color:#e8e8e8;border-left:2px solid #ff9500}
.bubble-wrap.src-cron .bubble-sender{color:#ff9500}
.bubble-wrap.src-cron .bubble-src{background:#3a2400;color:#ff9500}
/* System/restart — left, red-orange */
.bubble-wrap.src-restart .bubble{background:#1e0e00;border-bottom-left-radius:5px;color:#e8e8e8;border-left:2px solid #ff6b35}
.bubble-wrap.src-restart .bubble-sender{color:#ff6b35}
.bubble-wrap.src-restart .bubble-src{background:#3a1500;color:#ff6b35}
/* Source badge for user messages */
.bubble-wrap.user .bubble-src{background:rgba(255,255,255,.2);color:rgba(255,255,255,.85)}
.bubble-wrap.user .bubble-sender{color:rgba(255,255,255,.7)}

/* Chat input */
#chat-input-row{display:flex;padding:10px 12px;gap:8px;border-top:1px solid #2a2a2a;background:#1a1a1a;align-items:flex-end}
#chat-in{flex:1;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:20px;padding:8px 14px;color:#e8e8e8;font-size:14px;outline:none;resize:none;max-height:120px;font-family:inherit;line-height:1.4}
#chat-in:focus{border-color:#007aff}
#chat-btn{background:#007aff;border:none;border-radius:50%;width:36px;height:36px;color:#fff;font-size:18px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .2s}
#chat-btn:hover{background:#0066d6}
#chat-btn.offline{background:#333;cursor:default}

/* ── TERMINAL TAB ── */
#terminal{flex:1;overflow-y:auto;overflow-x:hidden;background:#0f0f0f;padding:12px 14px;font-family:'Courier New',Courier,monospace;font-size:12px;line-height:1.5;white-space:pre-wrap;overflow-wrap:break-word;word-break:break-word;color:#c9d1d9}
#terminal::-webkit-scrollbar{width:6px}
#terminal::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:3px}

/* ── SERVICES TAB ── */
#svc-list{flex:1;overflow-y:auto;padding:8px}
.svc{display:flex;align-items:center;padding:10px 8px;gap:10px;border-radius:10px;border-bottom:1px solid #1e1e1e}
.svc:last-child{border-bottom:none}
.sdot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sdot.on{background:#4cd964;box-shadow:0 0 5px #4cd96440}
.sdot.off{background:#ff3b30}
.sname{flex:1;font-size:13px;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sname.off{color:#555}
.rbtn{background:#2a2a2a;border:1px solid #3a3a3a;border-radius:8px;padding:4px 12px;font-size:11px;color:#888;cursor:pointer}
.rbtn:hover{border-color:#007aff;color:#007aff}

/* ── SETTINGS TAB ── */
#settings-panel{flex:1;overflow-y:auto;padding:16px}
.setting-row{display:flex;align-items:center;gap:10px;margin-bottom:12px;font-size:13px}
.setting-row label{width:120px;color:#888;flex-shrink:0}
.setting-row input,.setting-row select{flex:1;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:8px;padding:7px 10px;color:#e8e8e8;font-size:13px;outline:none}
.setting-row input:focus,.setting-row select:focus{border-color:#007aff}
#save-settings{background:#007aff;border:none;border-radius:8px;padding:8px 20px;color:#fff;font-size:13px;cursor:pointer;margin-top:4px}
#save-settings:hover{background:#0066d6}
</style>
</head>
<body>
<div id="app">
  <div id="header">
    <h1>Clawdy</h1>
    <div class="dot unknown" id="dot"></div>
    <span id="status-text">connecting</span>
    <span id="queue-badge"></span>
  </div>
  <div id="tabs">
    <div class="tab active" onclick="switchTab('chat')">Chat</div>
    <div class="tab" onclick="switchTab('terminal')">Terminal</div>
    <div class="tab" onclick="switchTab('services')">Services</div>
    <div class="tab" onclick="switchTab('settings')">Settings</div>
    <span id="ts-label"></span>
  </div>

  <!-- Chat panel -->
  <div id="panel-chat" class="panel active">
    <div id="chat-log">
      <button id="load-more" onclick="loadMore()">&#8593; Load earlier</button>
    </div>
    <div id="chat-input-row">
      <textarea id="chat-in" rows="1" placeholder="Message Claude..."></textarea>
      <button id="chat-btn" onclick="sendChat()">&#9650;</button>
    </div>
  </div>

  <!-- Terminal panel -->
  <div id="panel-terminal" class="panel">
    <pre id="terminal"></pre>
  </div>

  <!-- Services panel -->
  <div id="panel-services" class="panel">
    <div id="svc-list"></div>
  </div>

  <!-- Settings panel -->
  <div id="panel-settings" class="panel">
    <div id="settings-panel">
      <div class="setting-row">
        <label>Model</label>
        <select id="cfg-model">
          <option value="claude-haiku-4-5-20251001">Haiku 4.5 (fast)</option>
          <option value="claude-sonnet-4-6">Sonnet 4.6</option>
          <option value="claude-opus-4-6">Opus 4.6</option>
        </select>
      </div>
      <div class="setting-row">
        <label>Effort</label>
        <select id="cfg-effort">
          <option value="low">Low (faster)</option>
          <option value="medium">Medium</option>
          <option value="high">High (best)</option>
        </select>
      </div>
      <div class="setting-row">
        <label>Bot Name</label>
        <input id="cfg-name" />
      </div>
      <button id="save-settings" onclick="saveSettings()">Save &amp; apply</button>
      <p style="margin-top:10px;font-size:11px;color:#555">Model + Effort changes take effect after Clawdy restart.</p>
    </div>
  </div>
</div>

<script>
const term = document.getElementById('terminal');
const chatLog = document.getElementById('chat-log');
const dot = document.getElementById('dot');
const statusText = document.getElementById('status-text');
const queueBadge = document.getElementById('queue-badge');
let _activeTab = 'chat';

// Tab switching
const TABS = ['chat','terminal','services','settings'];
function switchTab(name) {
  _activeTab = name;
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', TABS[i]===name));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  if (name === 'chat') setTimeout(()=>{ chatLog.scrollTop = chatLog.scrollHeight; }, 50);
  if (name === 'services') loadServices();
  if (name === 'settings') loadSettings();
}

// SSE tmux stream
let _claudeAlive = true;

function applyStatus(d) {
  _claudeAlive = d.alive;
  let cls = 'dot', label = d.status;
  if (!d.alive) { cls += ' offline'; label = 'offline'; }
  else if (d.status === 'busy') { cls += ' busy'; label = 'busy'; }
  dot.className = cls;
  statusText.textContent = label;
  document.getElementById('chat-btn').classList.toggle('offline', !d.alive);
  if (d.queue_depth > 0) {
    queueBadge.style.display = '';
    queueBadge.textContent = 'queue: ' + d.queue_depth;
  } else {
    queueBadge.style.display = 'none';
  }
}

// Single SSE stream pushes terminal, status, and new messages
const es = new EventSource('/stream');
es.addEventListener('terminal', (e) => {
  const d = JSON.parse(e.data);
  const atBottom = term.scrollHeight - term.scrollTop <= term.clientHeight + 60;
  term.textContent = d.content;
  if (atBottom) term.scrollTop = term.scrollHeight;
  document.getElementById('ts-label').textContent = new Date().toLocaleTimeString();
});
es.addEventListener('status', (e) => { applyStatus(JSON.parse(e.data)); });
es.addEventListener('message', (e) => {
  const msgs = JSON.parse(e.data);
  const atBottom = chatLog.scrollHeight - chatLog.scrollTop <= chatLog.clientHeight + 80;
  msgs.forEach(m => renderMsg(m));
  if (atBottom) chatLog.scrollTop = chatLog.scrollHeight;
});
es.onerror = () => { statusText.textContent = 'stream error'; dot.className = 'dot unknown'; };

// Chat history
let _lastTs = 0;
let _firstTs = 9999999999;
let _seenIds = new Set();

// source → {badge label, extra css classes on bubble-wrap}
const SRC_BADGES = {
  telegram:  'TG',
  dashboard: 'DASH',
  peer:      'PEER',
  cron:      'CRON',
  restart:   'SYS',
};

function esc(t) { return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function renderMarkdown(text) {
  let s = esc(text);
  // Bold
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic (but not inside bold)
  s = s.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  // Inline code
  s = s.replace(/`([^`]+)`/g, '<code style="background:#2a2a2a;padding:1px 5px;border-radius:4px;font-size:12px;font-family:monospace">$1</code>');
  // Headers → bold line
  s = s.replace(/^#{1,3} (.+)$/gm, '<strong>$1</strong>');
  return s;
}

function renderMsg(m, prepend=false) {
  const id = m.ts + '|' + m.dir + '|' + m.sender + '|' + (m.text||'').slice(0,20);
  if (_seenIds.has(id)) return;
  _seenIds.add(id);

  const src = m.source || '';
  const isUser = m.dir === 'in' && (src === 'telegram' || src === 'dashboard');
  const isClawdy = m.dir === 'out';
  const d = new Date(m.ts * 1000);
  const ts = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

  // CSS classes for positioning + source color
  let wrapCls = 'bubble-wrap ';
  if (isUser)       wrapCls += 'user';
  else if (isClawdy) wrapCls += 'left clawdy';
  else               wrapCls += 'left src-' + (src || 'system');

  const wrap = document.createElement('div');
  wrap.className = wrapCls;

  const badge = SRC_BADGES[src] ? '<span class="bubble-src">'+SRC_BADGES[src]+'</span>' : '';
  const sender = '<span class="bubble-sender">'+esc(m.sender)+'</span>';

  const metaHtml = isUser
    ? '<div class="bubble-meta">'+badge+sender+'</div>'
    : '<div class="bubble-meta">'+badge+sender+'</div>';

  wrap.innerHTML = metaHtml
    + '<div class="bubble">'+renderMarkdown(m.text||'')+'</div>'
    + '<div class="bubble-time">'+ts+'</div>';

  const loadMoreBtn = document.getElementById('load-more');
  if (prepend) {
    chatLog.insertBefore(wrap, loadMoreBtn.nextSibling);
  } else {
    chatLog.appendChild(wrap);
  }
  if (m.ts > _lastTs) _lastTs = m.ts;
  if (m.ts < _firstTs) _firstTs = m.ts;
}

function loadHistory() {
  fetch('/api/chat-history?limit=50').then(r=>r.json()).then(d=>{
    const msgs = d.messages || [];
    msgs.forEach(m => renderMsg(m));
    chatLog.scrollTop = chatLog.scrollHeight;
    if (msgs.length >= 50) document.getElementById('load-more').style.display = '';
  }).catch(()=>{});
}

function loadMore() {
  fetch('/api/chat-history?before='+_firstTs+'&limit=50').then(r=>r.json()).then(d=>{
    const msgs = (d.messages || []).reverse();
    const scrollBottom = chatLog.scrollHeight - chatLog.scrollTop;
    msgs.forEach(m => renderMsg(m, true));
    chatLog.scrollTop = chatLog.scrollHeight - scrollBottom;
    if ((d.messages||[]).length < 50) document.getElementById('load-more').style.display = 'none';
  }).catch(()=>{});
}

loadHistory();

// Chat send
function sendChat() {
  const inp = document.getElementById('chat-in');
  const msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';
  inp.style.height = '';
  fetch('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:msg, sender:'Ben (Dashboard)'})})
    .catch(e=>console.error(e));
}
const chatIn = document.getElementById('chat-in');
chatIn.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
chatIn.addEventListener('input', () => {
  chatIn.style.height = '';
  chatIn.style.height = Math.min(chatIn.scrollHeight, 120) + 'px';
});

// Services
function loadServices() {
  fetch('/api/services').then(r=>r.json()).then(d=>{
    document.getElementById('svc-list').innerHTML = d.services.map(s => `
      <div class="svc">
        <div class="sdot ${s.active?'on':'off'}"></div>
        <span class="sname ${s.active?'':'off'}" title="${s.name}">${s.name.replace('.service','')}</span>
        <button class="rbtn" onclick="restartSvc('${s.name}')">restart</button>
      </div>`).join('');
  }).catch(()=>{});
}
function restartSvc(name) {
  if (!confirm('Restart ' + name + '?')) return;
  fetch('/api/restart/' + name, {method:'POST'}).then(r=>r.json()).then(d=>{
    if (d.ok) setTimeout(loadServices, 2000);
    else alert('Restart failed: ' + d.error);
  });
}

// Settings
function loadSettings() {
  fetch('/api/settings').then(r=>r.json()).then(d=>{
    const s = d.settings || {};
    if (s.claude_model) document.getElementById('cfg-model').value = s.claude_model;
    if (s.claude_effort) document.getElementById('cfg-effort').value = s.claude_effort;
    document.getElementById('cfg-name').value = s.BOT_NAME || 'Clawdy';
  }).catch(()=>{});
}
function saveSettings() {
  const data = {
    claude_model:  document.getElementById('cfg-model').value,
    claude_effort: document.getElementById('cfg-effort').value,
    BOT_NAME:      document.getElementById('cfg-name').value,
  };
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)})
    .then(r=>r.json()).then(d=>{
      if (d.ok) alert('Saved! Restart Clawdy for model/effort to take effect.');
      else alert('Error: ' + d.error);
    });
}
loadSettings();
</script>
</body>
</html>"""


# ── Whisper ───────────────────────────────────────────────────────────────────

def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            log.info("Loading Whisper 'base' model...")
            _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
            log.info("Whisper model loaded.")
        except Exception as e:
            log.error(f"Failed to load Whisper model: {e}")
    return _whisper_model


def transcribe_voice(file_path: Path) -> str | None:
    model = get_whisper_model()
    if model is None:
        return None
    try:
        segments, info = model.transcribe(str(file_path), beam_size=5, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        log.info(f"Transcribed ({info.language}, {info.duration:.1f}s): {text[:80]!r}")
        if not text and info.language_probability < 0.7:
            segments, info = model.transcribe(str(file_path), beam_size=5, vad_filter=True, language="de")
            text = " ".join(seg.text.strip() for seg in segments).strip()
        return text if text else None
    except Exception as e:
        log.error(f"Transcription failed: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_env():
    env = {}
    if not ENV_FILE.exists():
        log.error(f"No .env at {ENV_FILE}")
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
                delay = 2 ** attempt
                log.warning(f"Telegram API error ({method}), retry in {delay}s: {e}")
                time.sleep(delay)
            else:
                log.error(f"Telegram API error ({method}) after {_retries} attempts: {e}")
    return {"ok": False}


def send_message(token, chat_id, text, **kwargs):
    return tg_request(token, "sendMessage", chat_id=chat_id, text=text, **kwargs)


def get_file_info(msg):
    if "document" in msg:
        d = msg["document"]
        return d["file_id"], d.get("file_name", "document"), d.get("file_size", 0)
    if "photo" in msg:
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
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    result = tg_request(token, "getFile", file_id=file_id)
    if not result.get("ok"):
        log.error(f"getFile failed: {result}")
        return None
    file_path = result["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_path = FILES_DIR / f"{timestamp}_{filename_hint}"
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        log.info(f"Downloaded to {local_path}")
        return local_path
    except Exception as e:
        log.error(f"File download failed: {e}")
        return None


# ── Typing indicator ──────────────────────────────────────────────────────────

def start_typing(chat_id, timeout=90):
    global _typing_thread
    stop_typing()
    _stop_typing_event.clear()
    STOP_TYPING.unlink(missing_ok=True)

    def _loop():
        deadline = time.time() + timeout
        while True:
            if STOP_TYPING.exists():
                STOP_TYPING.unlink(missing_ok=True)
                break
            if time.time() >= deadline:
                log.info("Typing indicator auto-stopped (timeout)")
                break
            tg_request(_bot_token, "sendChatAction", chat_id=chat_id, action="typing")
            if _stop_typing_event.wait(4):
                break

    _typing_thread = threading.Thread(target=_loop, daemon=True)
    _typing_thread.start()


def stop_typing():
    global _typing_thread
    if _typing_thread and _typing_thread.is_alive():
        _stop_typing_event.set()
        _typing_thread.join(timeout=2)
    _typing_thread = None


# ── Injection queue ───────────────────────────────────────────────────────────

def inject_to_claude(display_text: str) -> bool:
    """Inject pre-formatted text into the tmux Claude session (raw, no prefix added)."""
    log.info(f"Injecting: {display_text[:80]}")
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}", display_text],
            check=True
        )
        time.sleep(0.3)
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}", "", "Enter"],
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        log.error(f"tmux inject failed: {e}")
        return False


def log_chat_history(direction: str, sender: str, text: str, source: str = ""):
    """Append a message to the shared chat history file."""
    try:
        entry = json.dumps({"ts": time.time(), "dir": direction, "sender": sender,
                            "text": text, "source": source})
        with open(CHAT_HISTORY, "a") as f:
            f.write(entry + "\n")
    except Exception as e:
        log.debug(f"chat history write failed: {e}")


def persist_pending(item: dict):
    """Append a queue item to the pending-injections file (survives bridge restarts)."""
    try:
        with open(PENDING_QUEUE, "a") as f:
            f.write(json.dumps(item) + "\n")
    except Exception as e:
        log.debug(f"pending queue write failed: {e}")


def clear_pending():
    """Clear the pending queue file (called after successful injection)."""
    try:
        PENDING_QUEUE.write_text("")
    except Exception:
        pass


def load_pending_queue():
    """On startup, reload any uninjected messages from the pending queue file."""
    if not PENDING_QUEUE.exists():
        return
    try:
        lines = PENDING_QUEUE.read_text().splitlines()
        count = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                _inject_queue.put(item)
                count += 1
            except Exception:
                pass
        if count:
            log.info(f"Loaded {count} pending injection(s) from disk queue")
        PENDING_QUEUE.write_text("")  # clear after loading
    except Exception as e:
        log.warning(f"Failed to load pending queue: {e}")


def enqueue_injection(text: str, sender: str, source: str = "telegram"):
    """Queue a message for serialized injection. Thread-safe.

    Adds the appropriate trigger-rule prefix based on source:
    - telegram / dashboard → [TELEGRAM from {sender} | {ts}]: {text}
    - peer                 → [PEER from {sender} | {ts}]: {text}
    - cron                 → [CRON | {ts}] {text}
    - restart              → text injected as-is (plain "continue")
    - other                → text injected as-is
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    if source in ("telegram", "dashboard"):
        display = f"[TELEGRAM from {sender} | {ts}]: {text}"
    elif source == "cron":
        display = f"[CRON | {ts}] {text}"
    else:
        # peer (pre-formatted), restart ("continue"), and others pass through as-is
        display = text
    item = {"display": display, "sender": sender, "source": source}
    _inject_queue.put(item)
    persist_pending(item)
    log_chat_history("in", sender, text, source=source)
    log.debug(f"Queued [{source}] from {sender}: {text[:60]}")


def _wait_for_alive(max_wait: int = 300):
    """Wait until Claude Code UI is visible in the tmux pane.
    The pane command is always 'bash' (while-loop wrapper), so we check pane content
    for Claude Code's signature UI markers instead."""
    elapsed = 0
    while elapsed < max_wait:
        r = subprocess.run(
            ["tmux", "capture-pane", "-pt", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            pane = r.stdout
            if "esc to interrupt" in pane or "❯" in pane:
                return True
        log.info(f"Claude not ready — waiting ({elapsed}s elapsed)...")
        time.sleep(3)
        elapsed += 3
    log.warning("Claude never came online — dropping message")
    return False


def start_injector_thread():
    """Single background thread that drains the injection queue serially.
    Injects immediately when Claude is alive. Only waits if the session is dead."""
    def _loop():
        while True:
            try:
                item = _inject_queue.get(timeout=5)
                if _wait_for_alive():
                    inject_to_claude(item["display"])
                    # Brief gap to avoid tmux key collision on back-to-back messages
                    time.sleep(0.4)
                    # Rewrite pending file without the item just injected
                    # (simplest: clear whole file since queue order is maintained in-memory)
                    try:
                        remaining = list(_inject_queue.queue)
                        PENDING_QUEUE.write_text(
                            "\n".join(json.dumps(i) for i in remaining) + ("\n" if remaining else "")
                        )
                    except Exception:
                        pass
            except _queue_module.Empty:
                continue
            except Exception as e:
                log.error(f"Injector thread error: {e}")

    t = threading.Thread(target=_loop, daemon=True, name="injector")
    t.start()
    log.info("Injection queue started")


# ── Bridge + dashboard HTTP server ────────────────────────────────────────────

class ClawdyHandler(BaseHTTPRequestHandler):
    """Handles both the peer bridge (POST /inject) and the management dashboard."""

    def log_message(self, fmt, *args):
        log.debug(f"HTTP: {fmt % args}")

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/stream":
            self._serve_sse()
        elif self.path == "/api/services":
            self._serve_services()
        elif self.path == "/api/settings":
            self._serve_settings()
        elif self.path == "/api/activity":
            self._serve_activity()
        elif self.path == "/api/status":
            self._serve_status()
        elif self.path.startswith("/api/chat-history"):
            self._serve_chat_history()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if self.path == "/inject":
            # Peer bridge endpoint — requires API key auth
            self._handle_inject(data)
        elif self.path == "/chat":
            # Dashboard chat injection — no extra auth (Tailscale-gated)
            self._handle_chat(data)
        elif self.path == "/api/settings":
            self._update_settings(data)
        elif re.match(r"^/api/restart/[a-zA-Z0-9\-\.@]+$", self.path):
            self._handle_restart()
        elif self.path == "/api/restart-context":
            self._handle_restart_context(data)
        elif self.path == "/api/claude-start":
            self._handle_claude_start()
        else:
            self.send_response(404)
            self.end_headers()

    # ── Bridge endpoint (peer bot) ─────────────────────────────────────────

    def _handle_inject(self, data):
        bridge_key = _get_env("BRIDGE_API_KEY", "")
        auth = self.headers.get("X-API-Key", "")
        if bridge_key and auth != bridge_key:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        message = data.get("message", "").strip()
        sender = data.get("sender", "Peer")
        ts = data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M"))

        if not message:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No message")
            return

        log.info(f"Bridge inject from {sender}: {message[:80]}")
        # Pre-format with [PEER from ...] so Claude's PEER trigger rule fires.
        # Pass original message for chat history (not the display string).
        display = f"[PEER from {sender} | {ts}]: {message}"
        _inject_queue.put({"display": display, "sender": sender, "source": "peer"})
        log_chat_history("in", sender, message, source="peer")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"queued")

    # ── Dashboard endpoints ────────────────────────────────────────────────

    def _handle_chat(self, data):
        msg = data.get("message", "").strip()
        sender = data.get("sender", "Ben (Dashboard)")
        source = data.get("source", "dashboard")
        # Only allow known safe sources via this endpoint
        if source not in ("dashboard", "restart", "cron"):
            source = "dashboard"
        if not msg:
            self._json({"ok": False, "error": "empty"}, 400)
            return
        enqueue_injection(msg, sender, source=source)
        self._json({"ok": True, "queued": _inject_queue.qsize()})

    def _handle_restart_context(self, data):
        """MCP calls this before triggering a planned restart to store resume context."""
        global _restart_pending
        context = data.get("context", "").strip()
        if context:
            _restart_pending = context
            log.info(f"Restart context stored: {context[:80]}")
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "no context"}, 400)

    def _handle_restart(self):
        svc = self.path.split("/api/restart/", 1)[-1]
        # Special case: claude-code = kill tmux session + relaunch
        if svc in ("claude-code", "claude-code.service"):
            self._handle_claude_start()
            return
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", svc],
                check=True, timeout=15, capture_output=True
            )
            self._json({"ok": True, "restarted": svc})
        except subprocess.CalledProcessError as e:
            self._json({"ok": False, "error": e.stderr.decode()[:200]}, 500)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _handle_claude_start(self):
        """Kill the Claude tmux session and relaunch via claude-start.sh."""
        try:
            subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION], capture_output=True)
            time.sleep(1)
            start_sh = Path.home() / "claude-start.sh"
            subprocess.Popen([str(start_sh)], start_new_session=True)
            self._json({"ok": True, "restarted": "claude-code"})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _serve_chat_history(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        since = float(qs.get("since", ["0"])[0])
        before = float(qs.get("before", ["9999999999"])[0])
        limit = int(qs.get("limit", ["50"])[0])

        messages = []
        try:
            if CHAT_HISTORY.exists():
                for line in CHAT_HISTORY.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                        if m.get("ts", 0) > since and m.get("ts", 0) < before:
                            messages.append(m)
                    except Exception:
                        pass
        except Exception:
            pass

        # Most recent N if fetching initial load (since=0), chronological
        if since == 0:
            messages = messages[-limit:]
        else:
            messages = messages[:limit]

        self._json({"messages": messages, "has_more": False})

    def _serve_html(self):
        body = DASHBOARD_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_status_data(self):
        try:
            r = subprocess.run(
                ["tmux", "has-session", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                capture_output=True, timeout=3
            )
            alive = r.returncode == 0
        except Exception:
            alive = False
        try:
            status = STATUS_FILE.read_text().strip() if alive else "offline"
        except Exception:
            status = "idle" if alive else "offline"
        return {"alive": alive, "status": status, "queue_depth": _inject_queue.qsize()}

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def push(event, data):
            msg = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(msg)
            self.wfile.flush()

        last_term = ""
        last_status = None
        last_chat_ts = 0.0
        tick = 0
        try:
            # Send initial status immediately
            st = self._get_status_data()
            push("status", st)
            last_status = st

            while True:
                # Terminal (every tick = 0.5s)
                r = subprocess.run(
                    ["tmux", "capture-pane", "-pt", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                    capture_output=True, text=True, timeout=5
                )
                raw = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^[Oc]', '', r.stdout)
                term_content = "\n".join(line.rstrip() for line in raw.splitlines()).strip()
                if term_content != last_term:
                    push("terminal", {"content": term_content})
                    last_term = term_content

                # Status (every 6 ticks = 3s)
                if tick % 6 == 0:
                    st = self._get_status_data()
                    if st != last_status:
                        push("status", st)
                        last_status = st

                # New chat messages (every 4 ticks = 2s)
                if tick % 4 == 0 and CHAT_HISTORY.exists():
                    new_msgs = []
                    try:
                        for line in CHAT_HISTORY.read_text().splitlines():
                            if not line.strip():
                                continue
                            try:
                                m = json.loads(line)
                                if m.get("ts", 0) > last_chat_ts:
                                    new_msgs.append(m)
                                    last_chat_ts = max(last_chat_ts, m["ts"])
                            except Exception:
                                pass
                    except Exception:
                        pass
                    if new_msgs:
                        push("message", new_msgs)

                tick += 1
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.debug(f"SSE stream ended: {e}")

    def _serve_services(self):
        # Core services always shown
        services = ["claude-code.service", "clawdy-bridge.service"]
        # Auto-discover project services
        try:
            r = subprocess.run(
                ["systemctl", "list-units", "--no-legend", "--no-pager",
                 "-t", "service", "--state=loaded"],
                capture_output=True, text=True, timeout=10
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if parts:
                    name = parts[0]
                    if any(x in name for x in ("meme-scanner", "fomofollow", "clawdy-")):
                        if name not in services:
                            services.append(name)
        except Exception:
            pass

        statuses = []
        for svc in services:
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5
                )
                statuses.append({"name": svc, "active": r.stdout.strip() == "active"})
            except Exception:
                statuses.append({"name": svc, "active": False})
        self._json({"services": statuses})

    def _serve_settings(self):
        # Expose non-secret env settings
        hidden = {"TELEGRAM_BOT_TOKEN", "BRIDGE_API_KEY"}
        settings = {}
        try:
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k not in hidden:
                        settings[k] = v.strip()
        except Exception:
            pass
        # Also read actual Claude model + effort from settings.json
        try:
            claude_settings_file = Path.home() / ".claude" / "settings.json"
            cs = json.loads(claude_settings_file.read_text())
            settings["claude_model"] = cs.get("model", "claude-sonnet-4-6")
            settings["claude_effort"] = cs.get("effortLevel", "medium")
        except Exception:
            pass
        self._json({"settings": settings})

    def _update_settings(self, data):
        updated = []
        # Write model + effort to ~/.claude/settings.json
        claude_keys = {"claude_model", "claude_effort"}
        claude_data = {k: v for k, v in data.items() if k in claude_keys}
        if claude_data:
            try:
                claude_settings_file = Path.home() / ".claude" / "settings.json"
                cs = json.loads(claude_settings_file.read_text()) if claude_settings_file.exists() else {}
                if "claude_model" in claude_data:
                    cs["model"] = claude_data["claude_model"]
                    updated.append("claude_model")
                if "claude_effort" in claude_data:
                    cs["effortLevel"] = claude_data["claude_effort"]
                    updated.append("claude_effort")
                claude_settings_file.write_text(json.dumps(cs, indent=2))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
                return
        # Write remaining safe keys to .env
        safe_keys = {"BOT_NAME", "BOT_PURPOSE", "LOG_LEVEL"}
        try:
            env_text = ENV_FILE.read_text()
            for k, v in data.items():
                if k not in safe_keys:
                    continue
                if re.search(f"^{k}=", env_text, re.MULTILINE):
                    env_text = re.sub(f"^{k}=.*$", f"{k}={v}", env_text, flags=re.MULTILINE)
                else:
                    env_text += f"\n{k}={v}"
                updated.append(k)
            ENV_FILE.write_text(env_text)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": True, "updated": updated})

    def _serve_activity(self):
        try:
            lines = ACTIVITY_LOG.read_text().splitlines()
            recent = "\n".join(lines[-100:])
        except Exception:
            recent = ""
        self._json({"activity": recent})

    def _serve_status(self):
        # Check if tmux session + Claude process are alive
        try:
            r = subprocess.run(
                ["tmux", "has-session", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                capture_output=True, timeout=3
            )
            alive = r.returncode == 0
        except Exception:
            alive = False

        # Read status file (busy / idle)
        try:
            status = STATUS_FILE.read_text().strip() if alive else "offline"
        except Exception:
            status = "idle" if alive else "offline"

        self._json({
            "alive": alive,
            "status": status,
            "queue_depth": _inject_queue.qsize(),
        })

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# Cached env for use inside the handler (avoids re-reading file on every request)
_env_cache: dict = {}


def _get_env(key: str, default: str = "") -> str:
    return _env_cache.get(key, default)


def start_combined_server(api_key: str, port: int, host: str):
    """Start the combined bridge + dashboard server on the given host:port."""
    server = ThreadingHTTPServer((host, port), ClawdyHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="http-server")
    t.start()
    log.info(f"Bridge + dashboard server on {host}:{port}")


# ── Telegram polling ──────────────────────────────────────────────────────────

def request_approval(token, admin_chat_id, new_chat_id, sender_name):
    msg = (
        f"New Telegram chat requesting Clawdy access:\n"
        f"Name: {sender_name}\n"
        f"Chat ID: {new_chat_id}\n\n"
        f"To allow: /allow {new_chat_id}"
    )
    send_message(token, admin_chat_id, msg)


def get_updates(token, offset=None, poll_timeout=30):
    params = {"timeout": poll_timeout, "allowed_updates": ["message"]}
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
                log.warning(f"getUpdates error, retry in {delay}s: {e}")
                time.sleep(delay)
            else:
                log.error(f"getUpdates failed after 3 attempts: {e}")
    return {"ok": False, "result": []}


# ── Main ──────────────────────────────────────────────────────────────────────

def start_crash_monitor():
    """Background thread: detects unexpected Claude crashes and queues a crash recovery message."""
    import time as _time

    def _loop():
        global _restart_pending
        was_alive = True
        _time.sleep(30)  # Give Claude time to start on first boot
        while True:
            try:
                r = subprocess.run(
                    ["tmux", "has-session", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                    capture_output=True, timeout=3
                )
                is_alive = r.returncode == 0
            except Exception:
                is_alive = False

            if was_alive and not is_alive:
                # Claude just went offline — clear any stale restart_pending set by this session
                _restart_pending = None

            if not was_alive and is_alive:
                # Claude just came back online — check for queued restart context file
                resume_file = Path.home() / ".easyclaw" / "restart-resume"
                if resume_file.exists():
                    try:
                        resume = resume_file.read_text().strip()
                        resume_file.unlink(missing_ok=True)
                        if resume:
                            log.info(f"Injecting restart context: {resume[:60]}")
                            # Small delay to let Claude Code fully init before injecting
                            _time.sleep(5)
                            enqueue_injection(resume, "system", source="restart")
                    except Exception as e:
                        log.warning(f"Failed to read restart-resume: {e}")

            was_alive = is_alive
            _time.sleep(5)

    t = threading.Thread(target=_loop, daemon=True, name="crash-monitor")
    t.start()
    log.info("Crash monitor started")


def main():
    global _bot_token, FILES_DIR, _env_cache

    log.info("Clawdy Telegram Bot starting...")
    env = load_env()
    _env_cache = env

    token = env.get("TELEGRAM_BOT_TOKEN", "")
    _bot_token = token

    FILES_DIR = Path(env["TELEGRAM_FILES_DIR"]) if env.get("TELEGRAM_FILES_DIR") else Path.home() / "telegram-files"

    if not token or token == "your_bot_token_here":
        log.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    cfg = load_config()

    # Parse allowed chats from env
    env_allowed = [c.strip() for c in env.get("TELEGRAM_ALLOWED_CHATS", "").split(",") if c.strip()]
    if env_allowed:
        cfg["allowed_chats"] = list(set(cfg["allowed_chats"] + [int(c) for c in env_allowed if c.isdigit()]))
        save_config(cfg)

    # Verify bot token
    me = tg_request(token, "getMe")
    if not me.get("ok"):
        log.error(f"Bot token invalid: {me}")
        sys.exit(1)
    log.info(f"Bot @{me['result']['username']} connected. Allowed: {cfg['allowed_chats']}")

    # Reload any messages queued before last bridge restart
    load_pending_queue()

    # Start injection queue (serializes all tmux send-keys calls)
    start_injector_thread()
    start_crash_monitor()

    # Start combined bridge + dashboard HTTP server
    bridge_key = env.get("BRIDGE_API_KEY", "")
    # Use BRIDGE_PORT for the combined server so peer bots can still reach /inject
    # DASHBOARD_PORT is kept as a fallback alias for the same port
    dashboard_port = int(env.get("BRIDGE_PORT", env.get("DASHBOARD_PORT", "8765")))
    try:
        import subprocess as _sp
        ts_ip = _sp.check_output(["tailscale", "ip", "-4"], text=True).strip()
    except Exception:
        ts_ip = "0.0.0.0"

    start_combined_server(bridge_key, dashboard_port, "0.0.0.0")
    log.info(f"Dashboard available at http://{ts_ip}:{dashboard_port}")

    # Main Telegram polling loop
    offset = None
    while True:
        data = get_updates(token, offset)
        if not data.get("ok"):
            time.sleep(5)
            continue

        # Collect-window: wait briefly then do a non-blocking follow-up poll
        # to catch split long messages before injecting
        if data.get("result"):
            time.sleep(0.3)
            next_offset = data["result"][-1]["update_id"] + 1
            followup = get_updates(token, next_offset, poll_timeout=0)
            if followup.get("result"):
                data["result"].extend(followup["result"])

        # Group messages by chat for batched injection
        to_inject: dict[int, dict] = {}

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue

            chat_id = msg["chat"]["id"]
            sender = msg["from"].get("first_name", "Unknown")
            text = msg.get("text", "")
            caption = msg.get("caption", "")

            if not text:
                file_id, filename_hint, file_size = get_file_info(msg)
                if file_id and chat_id in cfg["allowed_chats"]:
                    if file_size and file_size > FILE_SIZE_LIMIT:
                        send_message(token, chat_id, f"File too large ({file_size // (1024*1024)} MB). Max 20 MB.")
                        continue
                    is_voice = filename_hint == "voice.ogg" or "voice" in msg
                    if is_voice:
                        send_message(token, chat_id, "Transcribing voice message...")
                    else:
                        send_message(token, chat_id, f"Downloading {filename_hint}...")
                    local_path = download_file(token, file_id, filename_hint)
                    if local_path:
                        if is_voice:
                            transcribed = transcribe_voice(local_path)
                            if transcribed:
                                text = transcribed + (f" {caption}" if caption else "")
                            else:
                                send_message(token, chat_id, "Could not transcribe voice message.")
                                continue
                        else:
                            text = f"[File received — use Read tool: {local_path}]"
                            if caption:
                                text += f" Caption: {caption}"
                    else:
                        send_message(token, chat_id, "Failed to download file. Try again.")
                        continue
                elif chat_id in cfg["allowed_chats"]:
                    send_message(token, chat_id, "Unsupported message type.")
                    continue
                else:
                    continue

            # /allow command
            if text.startswith("/allow ") and chat_id in cfg["allowed_chats"]:
                new_id = text.split()[-1]
                if new_id.lstrip("-").isdigit():
                    cfg["allowed_chats"].append(int(new_id))
                    save_config(cfg)
                    send_message(token, chat_id, f"Chat {new_id} added.")
                continue

            # First-ever message — auto-register owner
            if not cfg["allowed_chats"]:
                log.info(f"First message from {sender} ({chat_id}) — registering as owner")
                cfg["allowed_chats"].append(chat_id)
                save_config(cfg)
                send_message(token, chat_id,
                    f"Hi {sender}! Registered your chat as owner (ID: {chat_id}).")
                env_text = ENV_FILE.read_text()
                env_text = env_text.replace("TELEGRAM_CHAT_ID=", f"TELEGRAM_CHAT_ID={chat_id}")
                env_text = env_text.replace("TELEGRAM_ALLOWED_CHATS=", f"TELEGRAM_ALLOWED_CHATS={chat_id}")
                ENV_FILE.write_text(env_text)
                continue

            if chat_id not in cfg["allowed_chats"]:
                log.warning(f"Unauthorized chat {chat_id} ({sender}): {text[:50]}")
                if cfg["allowed_chats"]:
                    request_approval(token, cfg["allowed_chats"][0], chat_id, sender)
                send_message(token, chat_id, "Not authorized.")
                continue

            log.info(f"Message from {sender} ({chat_id}): {text[:80]}")

            # Rate limiting
            now = time.time()
            _rate_limit.setdefault(chat_id, [])
            _rate_limit[chat_id] = [t for t in _rate_limit[chat_id] if now - t < 30]
            if len(_rate_limit[chat_id]) >= 5:
                send_message(token, chat_id, "Too fast — max 5 messages per 30 seconds.")
                continue
            _rate_limit[chat_id].append(now)

            if chat_id not in to_inject:
                to_inject[chat_id] = {"sender": sender, "texts": []}
            to_inject[chat_id]["texts"].append(text)

        # Inject each chat's messages
        for chat_id, batch in to_inject.items():
            combined = "\n\n".join(batch["texts"])
            if len(batch["texts"]) > 1:
                log.info(f"Combining {len(batch['texts'])} parts for chat {chat_id}")
            start_typing(chat_id)
            enqueue_injection(combined, batch["sender"], source="telegram")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopped.")
