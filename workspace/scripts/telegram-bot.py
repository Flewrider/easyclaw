#!/usr/bin/env python3
"""
Clawdy Telegram Bot Bridge + Management Dashboard
- Polls Telegram for new messages from authorized chats
- Injects them into the tmux Claude session via a serialized queue
  (prevents race conditions when Telegram + peer messages arrive simultaneously)
- Serves a management dashboard via HTTP (Tailscale-only, DASHBOARD_PORT)
- Run as a systemd service: clawdy-telegram-bot.service
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
LOG_FILE = EASYCLAW / "telegram-bot.log"
STOP_TYPING = EASYCLAW / "stop-typing"
STATUS_FILE = EASYCLAW / "status"
ACTIVITY_LOG = EASYCLAW / "activity-log.md"
CHAT_HISTORY = EASYCLAW / "chat-history.jsonl"
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
body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',system-ui,sans-serif;height:100vh;overflow:hidden}
#app{display:flex;flex-direction:column;height:100vh}
#header{background:#161b22;border-bottom:1px solid #30363d;padding:8px 16px;display:flex;align-items:center;gap:12px;flex-shrink:0}
#header h1{font-size:15px;font-weight:600;color:#58a6ff;letter-spacing:.3px}
.dot{width:8px;height:8px;border-radius:50%;background:#3fb950;flex-shrink:0;transition:background .4s}
.dot.busy{background:#e3b341}
.dot.offline{background:#f78166}
.dot.unknown{background:#8b949e}
#status-text{font-size:12px;color:#8b949e}
#queue-badge{background:#30363d;border-radius:10px;padding:2px 8px;font-size:11px;color:#e3b341;display:none}
#ts-label{margin-left:auto;font-size:11px;color:#484f58}
#main{display:flex;flex:1;min-height:0}
#terminal-pane{flex:1;display:flex;flex-direction:column;min-width:0}
#terminal{flex:1;overflow-y:auto;overflow-x:hidden;background:#0d1117;padding:12px 14px;font-family:'Courier New',Courier,monospace;font-size:12px;line-height:1.45;white-space:pre-wrap;overflow-wrap:break-word;word-break:break-word;color:#c9d1d9}
#terminal::-webkit-scrollbar{width:6px}
#terminal::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
#sidebar{width:360px;flex-shrink:0;display:flex;flex-direction:column;border-left:1px solid #30363d;background:#0d1117}
@media(max-width:700px){
  #main{flex-direction:column}
  #terminal-pane{height:45vh;flex:none}
  #sidebar{width:100%;border-left:none;border-top:1px solid #30363d;flex:1}
}
.section-hdr{padding:7px 12px;background:#161b22;border-bottom:1px solid #30363d;font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none}
#chat-section{flex:1;display:flex;flex-direction:column;min-height:0}
#chat-log{flex:1;overflow-y:auto;padding:6px 8px}
#chat-log::-webkit-scrollbar{width:4px}
#chat-log::-webkit-scrollbar-thumb{background:#30363d}
.msg{padding:4px 8px;border-radius:4px;margin-bottom:3px;font-size:12px;line-height:1.4;background:#1c2128;border-left:2px solid #58a6ff}
.msg.out{border-left-color:#3fb950;background:#1a2b1a}
.msg .ts{color:#484f58;font-size:10px;margin-right:4px}
.msg .src{font-size:10px;margin-right:4px;font-weight:600}
.msg.in .src{color:#58a6ff}
.msg.out .src{color:#3fb950}
.src-badge{font-size:9px;background:#30363d;border-radius:3px;padding:1px 4px;margin-right:4px;color:#8b949e;font-weight:600;letter-spacing:.3px}
#load-more{display:none;width:100%;background:#21262d;border:1px solid #30363d;border-radius:4px;padding:4px;color:#8b949e;font-size:11px;cursor:pointer;margin-bottom:4px}
#load-more:hover{color:#c9d1d9}
#chat-row{display:flex;padding:8px;gap:6px;border-top:1px solid #30363d}
#chat-in{flex:1;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:6px 10px;color:#c9d1d9;font-size:13px;outline:none}
#chat-in:focus{border-color:#58a6ff}
#chat-btn{background:#238636;border:none;border-radius:6px;padding:6px 14px;color:#fff;font-size:13px;cursor:pointer;white-space:nowrap}
#chat-btn:hover{background:#2ea043}
#services-section{flex-shrink:0;border-top:1px solid #30363d}
#svc-list{padding:4px 8px 6px;max-height:180px;overflow-y:auto}
.svc{display:flex;align-items:center;padding:4px 4px;gap:8px;border-radius:4px}
.svc:hover{background:#161b22}
.sdot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sdot.on{background:#3fb950}
.sdot.off{background:#f78166}
.sname{flex:1;font-size:11px;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#c9d1d9}
.sname.off{color:#8b949e}
.rbtn{background:#21262d;border:1px solid #30363d;border-radius:4px;padding:2px 8px;font-size:10px;color:#8b949e;cursor:pointer}
.rbtn:hover{border-color:#58a6ff;color:#58a6ff}
#settings-section{flex-shrink:0;border-top:1px solid #30363d}
#settings-body{padding:8px;display:none}
.setting-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px}
.setting-row label{width:110px;color:#8b949e;flex-shrink:0}
.setting-row input,.setting-row select{flex:1;background:#161b22;border:1px solid #30363d;border-radius:4px;padding:4px 8px;color:#c9d1d9;font-size:12px;outline:none}
.setting-row input:focus,.setting-row select:focus{border-color:#58a6ff}
#save-settings{background:#21262d;border:1px solid #30363d;border-radius:4px;padding:3px 12px;color:#c9d1d9;font-size:12px;cursor:pointer;margin-top:4px}
#save-settings:hover{border-color:#58a6ff;color:#58a6ff}
</style>
</head>
<body>
<div id="app">
  <div id="header">
    <h1>Clawdy</h1>
    <div class="dot unknown" id="dot"></div>
    <span id="status-text">connecting</span>
    <span id="queue-badge"></span>
    <span id="ts-label"></span>
  </div>
  <div id="main">
    <div id="terminal-pane"><pre id="terminal"></pre></div>
    <div id="sidebar">
      <div id="chat-section">
        <div class="section-hdr">Chat</div>
        <div id="chat-log"><button id="load-more" onclick="loadMore()">&#8593; Load earlier</button></div>
        <div id="chat-row">
          <input id="chat-in" placeholder="Message Claude..." />
          <button id="chat-btn" onclick="sendChat()">Send</button>
        </div>
      </div>
      <div id="services-section">
        <div class="section-hdr" onclick="toggleSection('svc-list','svc-arrow')">
          Services <span id="svc-arrow">&#9660;</span>
        </div>
        <div id="svc-list"></div>
      </div>
      <div id="settings-section">
        <div class="section-hdr" onclick="toggleSection('settings-body','cfg-arrow')">
          Settings <span id="cfg-arrow">&#9660;</span>
        </div>
        <div id="settings-body" style="display:none">
          <div class="setting-row">
            <label>Model</label>
            <select id="cfg-model">
              <option value="haiku">Haiku (fast)</option>
              <option value="sonnet">Sonnet</option>
              <option value="opus">Opus</option>
            </select>
          </div>
          <div class="setting-row">
            <label>Bot Name</label>
            <input id="cfg-name" />
          </div>
          <button id="save-settings" onclick="saveSettings()">Save &amp; apply</button>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
const term=document.getElementById('terminal');
const chatLog=document.getElementById('chat-log');
const dot=document.getElementById('dot');
const statusText=document.getElementById('status-text');
const queueBadge=document.getElementById('queue-badge');

// SSE tmux stream
const es=new EventSource('/stream');
es.onmessage=(e)=>{
  const d=JSON.parse(e.data);
  const atBottom=term.scrollHeight-term.scrollTop<=term.clientHeight+60;
  term.textContent=d.content;
  if(atBottom) term.scrollTop=term.scrollHeight;
  document.getElementById('ts-label').textContent=new Date().toLocaleTimeString();
};
es.onerror=()=>{statusText.textContent='stream error';dot.className='dot unknown';};

// Status polling
let _claudeAlive = true;
function pollStatus(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    _claudeAlive = d.alive;
    let cls = 'dot', label = d.status;
    if (!d.alive) { cls += ' offline'; label = 'offline'; }
    else if (d.status === 'busy') { cls += ' busy'; label = 'busy'; }
    // else idle/ready — green dot (default)
    dot.className = cls;
    statusText.textContent = label;
    // Update send button state
    const btn = document.getElementById('chat-btn');
    btn.textContent = !d.alive ? 'Offline' : (d.queue_depth > 0 ? 'Queued ('+d.queue_depth+')' : 'Send');
    btn.style.background = !d.alive ? '#30363d' : '';
    if(d.queue_depth>0){
      queueBadge.style.display='';
      queueBadge.textContent='queue: '+d.queue_depth;
    } else {
      queueBadge.style.display='none';
    }
  }).catch(()=>{});
}
setInterval(pollStatus,3000);
pollStatus();

// Chat history
let _lastTs = 0;
let _firstTs = 9999999999;
let _seenIds = new Set();

const SRC_LABELS = {telegram:'TG', dashboard:'DB', peer:'PEER', cron:'CRON', '':'', out:'OUT'};
function renderMsg(m, prepend=false) {
  const id = m.ts + m.dir + m.sender;
  if (_seenIds.has(id)) return;
  _seenIds.add(id);
  const el = document.createElement('div');
  el.className = 'msg ' + (m.dir === 'out' ? 'out' : 'in');
  const d = new Date(m.ts * 1000);
  const ts = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  const srcLabel = m.source ? (SRC_LABELS[m.source]||m.source.toUpperCase()) : '';
  const srcBadge = srcLabel ? '<span class="src-badge">'+srcLabel+'</span>' : '';
  el.innerHTML = '<span class="ts">'+ts+'</span>'+srcBadge+'<span class="src">'+esc(m.sender)+'</span>'+esc(m.text);
  const loadMoreBtn = document.getElementById('load-more');
  if (prepend) {
    chatLog.insertBefore(el, loadMoreBtn.nextSibling);
  } else {
    chatLog.appendChild(el);
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

function pollNewMessages() {
  if (_lastTs === 0) return; // wait for initial load
  fetch('/api/chat-history?since='+_lastTs+'&limit=100').then(r=>r.json()).then(d=>{
    const msgs = d.messages || [];
    if (msgs.length > 0) {
      const atBottom = chatLog.scrollHeight - chatLog.scrollTop <= chatLog.clientHeight + 60;
      msgs.forEach(m => renderMsg(m));
      if (atBottom) chatLog.scrollTop = chatLog.scrollHeight;
    }
  }).catch(()=>{});
}

loadHistory();
setInterval(pollNewMessages, 2000);

// Chat send
function sendChat(){
  const inp=document.getElementById('chat-in');
  const msg=inp.value.trim();
  if(!msg)return;
  inp.value='';
  fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,sender:'Ben (Dashboard)'})})
    .catch(e=>console.error(e));
}
document.getElementById('chat-in').addEventListener('keydown',(e)=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}
});
function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// Services
function loadServices(){
  fetch('/api/services').then(r=>r.json()).then(d=>{
    document.getElementById('svc-list').innerHTML=d.services.map(s=>`
      <div class="svc">
        <div class="sdot ${s.active?'on':'off'}"></div>
        <span class="sname ${s.active?'':'off'}" title="${s.name}">${s.name.replace('.service','')}</span>
        <button class="rbtn" onclick="restartSvc('${s.name}')">restart</button>
      </div>`).join('');
  }).catch(()=>{});
}
function restartSvc(name){
  if(!confirm('Restart '+name+'?'))return;
  fetch('/api/restart/'+name,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok)setTimeout(loadServices,2000);
    else alert('Restart failed: '+d.error);
  });
}
loadServices();
setInterval(loadServices,15000);

// Settings
function loadSettings(){
  fetch('/api/settings').then(r=>r.json()).then(d=>{
    const s=d.settings||{};
    document.getElementById('cfg-model').value=s.CLAUDE_DEFAULT_MODEL||'haiku';
    document.getElementById('cfg-name').value=s.BOT_NAME||'Clawdy';
  }).catch(()=>{});
}
function saveSettings(){
  const data={
    CLAUDE_DEFAULT_MODEL:document.getElementById('cfg-model').value,
    BOT_NAME:document.getElementById('cfg-name').value,
  };
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
    .then(r=>r.json()).then(d=>{
      if(d.ok) alert('Settings saved. Restart Clawdy for model change to take effect.');
      else alert('Error: '+d.error);
    });
}
loadSettings();

// Collapse/expand sections
function toggleSection(bodyId,arrowId){
  const body=document.getElementById(bodyId);
  const arrow=document.getElementById(arrowId);
  const nowVisible=body.style.display!=='none';
  body.style.display=nowVisible?'none':'';
  arrow.innerHTML=nowVisible?'&#9658;':'&#9660;';
}
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


def enqueue_injection(text: str, sender: str, source: str = "telegram"):
    """Queue a message for serialized injection. Thread-safe.

    Adds the appropriate trigger-rule prefix based on source:
    - telegram / dashboard → [TELEGRAM from {sender} | {ts}]: {text}
    - peer                 → text already pre-formatted as [PEER from ...]
    - cron / other         → text already pre-formatted by caller
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    if source in ("telegram", "dashboard"):
        display = f"[TELEGRAM from {sender} | {ts}]: {text}"
    else:
        # peer, cron, and restart context are pre-formatted by their callers
        display = text
    _inject_queue.put({"display": display, "sender": sender, "source": source})
    log_chat_history("in", sender, text, source=source)
    log.debug(f"Queued [{source}] from {sender}: {text[:60]}")


def _wait_for_alive(max_wait: int = 300):
    """Wait until the Claude tmux session exists. Only blocks if Claude is offline."""
    elapsed = 0
    while elapsed < max_wait:
        r = subprocess.run(
            ["tmux", "has-session", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
            capture_output=True
        )
        if r.returncode == 0:
            return True
        log.info(f"Claude offline — waiting to inject ({elapsed}s elapsed)...")
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
        # Use [PEER from ...] prefix so Claude's PEER trigger rule fires
        display = f"[PEER from {sender} | {ts}]: {message}"
        enqueue_injection(display, sender, source="peer")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"queued")

    # ── Dashboard endpoints ────────────────────────────────────────────────

    def _handle_chat(self, data):
        msg = data.get("message", "").strip()
        sender = data.get("sender", "Ben (Dashboard)")
        if not msg:
            self._json({"ok": False, "error": "empty"}, 400)
            return
        enqueue_injection(msg, sender, source="dashboard")
        self._json({"ok": True, "queued": _inject_queue.qsize()})

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

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = ""
        try:
            while True:
                r = subprocess.run(
                    ["tmux", "capture-pane", "-pt", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                    capture_output=True, text=True, timeout=5
                )
                # Strip ANSI escape codes and trailing whitespace per line
                raw = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^[Oc]', '', r.stdout)
                content = "\n".join(line.rstrip() for line in raw.splitlines()).strip()
                if content != last:
                    payload = json.dumps({"content": content})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    last = content
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.debug(f"SSE stream ended: {e}")

    def _serve_services(self):
        # Core services always shown
        services = ["claude-code.service", "clawdy-telegram-bot.service"]
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
        # Expose non-secret settings only
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
        self._json({"settings": settings})

    def _update_settings(self, data):
        safe_keys = {"CLAUDE_DEFAULT_MODEL", "BOT_NAME", "BOT_PURPOSE", "LOG_LEVEL"}
        updated = []
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

    # Start injection queue (serializes all tmux send-keys calls)
    start_injector_thread()

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

    start_combined_server(bridge_key, dashboard_port, ts_ip)
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
