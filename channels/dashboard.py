#!/usr/bin/env python3
"""
Clawdy Standalone Management Dashboard
- Serves the management dashboard via HTTP
- SSE stream for live terminal output, status updates, and new chat messages
- REST APIs for services, settings, secrets, crons, chat history
- Port: DASHBOARD_PORT env or 8766
- No dependency on clawdy-bridge.py
"""

import os
import re
import sys
import json
import time
import subprocess
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs

EASYCLAW = Path.home() / ".easyclaw"
ENV_FILE = EASYCLAW / ".env"
LOG_FILE = EASYCLAW / "dashboard.log"
STATUS_FILE = EASYCLAW / "status"
ACTIVITY_LOG = EASYCLAW / "activity-log.md"
CHAT_HISTORY = EASYCLAW / "chat-history.jsonl"

TMUX_SESSION = "claude"
TMUX_WINDOW = "claude"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
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
.bubble-wrap.src-peer-out{align-items:flex-end}
.bubble-wrap.src-peer-out .bubble{background:#1a4a2a;color:#e0f5e0}
.bubble-wrap.src-peer-out .bubble-sender{color:rgba(76,217,100,.75)}
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
#key-row{display:flex;gap:6px;padding:6px 12px 0;background:#1a1a1a}
.key-btn{background:#2a2a2a;border:1px solid #3a3a3a;border-radius:8px;color:#aaa;font-size:16px;padding:4px 14px;cursor:pointer;flex:1;transition:background .15s}
.key-btn:active{background:#007aff;color:#fff}
#chat-input-row{display:flex;padding:10px 12px;gap:8px;border-top:1px solid #2a2a2a;background:#1a1a1a;align-items:flex-end;padding-bottom:calc(10px + env(safe-area-inset-bottom))}
#ptt-btn{background:#1e1e1e;border:1px solid #333;border-radius:10px;color:#888;font-size:16px;padding:6px 10px;cursor:pointer;flex-shrink:0;transition:background .15s,color .15s,border-color .15s;user-select:none;-webkit-user-select:none;touch-action:none}
#typing-bubble{display:none;align-items:center;gap:5px;padding:4px 10px 10px 14px}
#typing-bubble .tdot{width:7px;height:7px;border-radius:50%;background:#555;animation:tdot-bounce 1.2s ease-in-out infinite}
#typing-bubble .tdot:nth-child(2){animation-delay:.2s}
#typing-bubble .tdot:nth-child(3){animation-delay:.4s}
@keyframes tdot-bounce{0%,80%,100%{transform:translateY(0);background:#555}40%{transform:translateY(-6px);background:#888}}
#ptt-btn.recording{background:#3d1010;border-color:#c0392b;color:#e74c3c;animation:ptt-pulse 1s ease-in-out infinite}
@keyframes ptt-pulse{0%,100%{box-shadow:0 0 0 0 rgba(231,76,60,.4)}50%{box-shadow:0 0 0 6px rgba(231,76,60,0)}}
#chat-in{flex:1;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:20px;padding:8px 14px;color:#e8e8e8;font-size:16px;outline:none;resize:none;max-height:120px;font-family:inherit;line-height:1.4}
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
.setting-row input,.setting-row select{flex:1;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:8px;padding:7px 10px;color:#e8e8e8;font-size:16px;outline:none}
.setting-row input:focus,.setting-row select:focus{border-color:#007aff}
.cron-card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:14px;margin-bottom:14px}
.cron-card h4{margin:0 0 10px;font-size:13px;color:#e8e8e8;display:flex;align-items:center;gap:8px}
.cron-card h4 .cron-badge{font-size:10px;background:#2a2a2a;color:#888;border-radius:5px;padding:2px 6px;font-weight:normal}
.cron-card textarea{width:100%;box-sizing:border-box;background:#0f0f0f;border:1px solid #2a2a2a;border-radius:8px;color:#c9d1d9;font-family:'Courier New',monospace;font-size:12px;padding:8px 10px;resize:vertical;min-height:80px;outline:none}
.cron-card textarea:focus{border-color:#007aff}
.cron-card .cron-footer{display:flex;align-items:center;gap:8px;margin-top:8px}
.cron-card .cron-footer input{flex:1;background:#2a2a2a;border:1px solid #3a3a3a;border-radius:8px;padding:5px 8px;color:#e8e8e8;font-size:12px;font-family:monospace;outline:none}
.cron-card .cron-footer input:focus{border-color:#007aff}
.cron-section-title{font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.08em;margin:18px 0 10px;padding-bottom:6px;border-bottom:1px solid #1e1e1e}
#save-settings{background:#007aff;border:none;border-radius:8px;padding:8px 20px;color:#fff;font-size:13px;cursor:pointer;margin-top:4px}
#save-settings:hover{background:#0066d6}
</style>
</head>
<body>
<div id="app">
  <div id="header">
    <h1 id="header-title">Clawdy</h1>
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
    <div id="typing-bubble"><div class="tdot"></div><div class="tdot"></div><div class="tdot"></div></div>
    <div id="key-row">
      <button class="key-btn" onclick="sendKey('Up')" title="Arrow Up">&#8593;</button>
      <button class="key-btn" onclick="sendKey('Down')" title="Arrow Down">&#8595;</button>
      <button class="key-btn" onclick="sendKey('Enter')" title="Enter">&#9166;</button>
    </div>
    <div id="chat-input-row">
      <button id="ptt-btn" title="Hold to talk (or hold Space when not typing)" onmousedown="pttStart(event)" onmouseup="pttStop(event)" ontouchstart="pttStart(event)" ontouchend="pttStop(event)">&#127908;</button>
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
          <option value="haiku">Haiku 4.5 (fast)</option>
          <option value="sonnet">Sonnet 4.6</option>
          <option value="opus">Opus 4.6 (1M context)</option>
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
      <div class="cron-section-title" style="margin-top:22px">Secrets</div>
      <p style="font-size:11px;color:#555;margin:4px 0 10px">Values are write-only — existing keys show as set. Leave blank to keep current value.</p>
      <div class="setting-row">
        <label>Telegram Bot Token</label>
        <div style="display:flex;align-items:center;gap:8px;flex:1">
          <input id="sec-telegram" type="password" placeholder="Enter new value…" style="flex:1" />
          <span id="sec-telegram-status" style="font-size:11px;color:#555;white-space:nowrap"></span>
        </div>
      </div>
      <div class="setting-row">
        <label>Groq API Key</label>
        <div style="display:flex;align-items:center;gap:8px;flex:1">
          <input id="sec-groq" type="password" placeholder="Enter new value…" style="flex:1" />
          <span id="sec-groq-status" style="font-size:11px;color:#555;white-space:nowrap"></span>
        </div>
      </div>
      <button onclick="saveSecrets()" style="margin-top:6px">Save secrets</button>
      <div class="cron-section-title" style="margin-top:22px">Cron Jobs</div>
      <div id="crons-list"></div>
      <button onclick="showAddCron()" style="margin-top:4px;font-size:11px;padding:4px 10px;width:auto">+ Add Cron</button>
      <div id="add-cron-form" style="display:none;margin-top:14px" class="cron-card">
        <h4>New Cron</h4>
        <div class="setting-row"><label>Name</label><input id="new-cron-name" placeholder="MY_CRON" /></div>
        <div class="setting-row"><label>Schedule</label><input id="new-cron-schedule" placeholder="*/30 * * * *" style="font-family:monospace" /></div>
        <div class="setting-row" style="align-items:flex-start"><label style="padding-top:6px">Instructions</label><textarea id="new-cron-content" style="flex:1;min-height:80px;background:#0f0f0f;border:1px solid #2a2a2a;border-radius:8px;color:#c9d1d9;font-family:'Courier New',monospace;font-size:12px;padding:8px;resize:vertical;outline:none" placeholder="What should Clawdy do when this fires?"></textarea></div>
        <div style="display:flex;gap:8px">
          <button onclick="addCron()">Create</button>
          <button onclick="document.getElementById('add-cron-form').style.display='none'" style="background:#2a2a2a">Cancel</button>
        </div>
      </div>
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
  if (name === 'terminal') setTimeout(()=>{ term.scrollTop = term.scrollHeight; }, 50);
  if (name === 'services') loadServices();
  if (name === 'settings') { loadSettings(); loadCrons(); loadSecrets(); }
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
let es, _sseConnected = false;
function connectSSE() {
  if (es && _sseConnected) return;
  if (es) { try { es.close(); } catch(_) {} }
  _sseConnected = false;
  // Warm up the Tailscale tunnel with a fast REST call first,
  // then open SSE so it reuses the established connection
  fetch('/api/status').catch(()=>{}).finally(() => {
    es = new EventSource('/stream');
  es.addEventListener('terminal', (e) => {
  const d = JSON.parse(e.data);
  const atBottom = term.scrollHeight - term.scrollTop <= term.clientHeight + 60;
  const prevScrollTop = term.scrollTop;
  const prevHeight = term.scrollHeight;
  term.textContent = d.content;
  if (atBottom) {
    term.scrollTop = term.scrollHeight;
  } else {
    // Preserve scroll position as content grows
    term.scrollTop = prevScrollTop + (term.scrollHeight - prevHeight);
  }
  document.getElementById('ts-label').textContent = new Date().toLocaleTimeString();
});
es.addEventListener('status', (e) => { applyStatus(JSON.parse(e.data)); });
es.addEventListener('typing', (e) => {
  const d = JSON.parse(e.data);
  document.getElementById('typing-bubble').style.display = d.active ? 'flex' : 'none';
  if (d.active && _activeTab === 'chat') chatLog.scrollTop = chatLog.scrollHeight;
});
es.addEventListener('message', (e) => {
  const msgs = JSON.parse(e.data);
  const atBottom = chatLog.scrollHeight - chatLog.scrollTop <= chatLog.clientHeight + 80;
  msgs.forEach(m => renderMsg(m));
  if (atBottom) chatLog.scrollTop = chatLog.scrollHeight;
});
    es.addEventListener('open', () => { _sseConnected = true; });
    es.onerror = () => {
      _sseConnected = false;
      statusText.textContent = 'stream error'; dot.className = 'dot unknown';
      es.close();
      setTimeout(connectSSE, 3000);
    };
  });
}
connectSSE();

// Chat history
let _lastTs = 0;
let _firstTs = 9999999999;
let _seenIds = new Set();

// source → {badge label, extra css classes on bubble-wrap}
const SRC_BADGES = {
  telegram:  'TG',
  dashboard: 'DASH',
  peer:      'PEER',
  'peer-out':'PEER',
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

function renderMsg(m) {
  const id = m.ts + '|' + m.dir + '|' + m.sender + '|' + (m.text||'').slice(0,20);
  if (_seenIds.has(id)) return;
  _seenIds.add(id);
  // Suppress SSE echo of optimistically-rendered dashboard messages
  if (m.source === 'dashboard' && m.dir === 'in') {
    const expiry = _optimisticTexts.get(m.text||'');
    if (expiry && Date.now() / 1000 < expiry) { _optimisticTexts.delete(m.text||''); return; }
  }

  const src = m.source || '';
  const isUser = m.dir === 'in' && (src === 'telegram' || src === 'dashboard');
  const isPeerOut = src === 'peer-out';
  const isClawdy = m.dir === 'out' && !isPeerOut;
  const d = new Date(m.ts * 1000);
  const ts = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});

  let wrapCls = 'bubble-wrap ';
  if (isUser)         wrapCls += 'user';
  else if (isPeerOut) wrapCls += 'user src-peer-out';
  else if (isClawdy)  wrapCls += 'left clawdy';
  else                wrapCls += 'left src-' + (src || 'system');

  const wrap = document.createElement('div');
  wrap.className = wrapCls;
  wrap.dataset.ts = m.ts;

  const badge = SRC_BADGES[src] ? '<span class="bubble-src">'+SRC_BADGES[src]+'</span>' : '';
  const sender = '<span class="bubble-sender">'+esc(m.sender)+'</span>';
  wrap.innerHTML = '<div class="bubble-meta">'+badge+sender+'</div>'
    + '<div class="bubble">'+renderMarkdown(m.text||'')+'</div>'
    + '<div class="bubble-time">'+ts+'</div>';

  // Insert in chronological order by scanning from the bottom
  const bubbles = chatLog.querySelectorAll('.bubble-wrap[data-ts]');
  let inserted = false;
  for (let i = bubbles.length - 1; i >= 0; i--) {
    if (m.ts >= parseFloat(bubbles[i].dataset.ts)) {
      bubbles[i].after(wrap);
      inserted = true;
      break;
    }
  }
  if (!inserted) {
    // Older than everything — insert after load-more button
    document.getElementById('load-more').after(wrap);
  }

  if (m.ts > _lastTs) _lastTs = m.ts;
  if (m.ts < _firstTs) _firstTs = m.ts;
}

function loadHistory() {
  // Show cached messages instantly while network loads
  try {
    const cached = JSON.parse(localStorage.getItem('clawdy_msgs') || '[]');
    if (cached.length) {
      cached.forEach(m => renderMsg(m));
      chatLog.scrollTop = chatLog.scrollHeight;
    }
  } catch(_) {}
  // Fetch fresh — update cache and fill any gaps
  fetch('/api/chat-history?limit=10').then(r=>r.json()).then(d=>{
    const msgs = d.messages || [];
    if (!msgs.length) return;
    try { localStorage.setItem('clawdy_msgs', JSON.stringify(msgs)); } catch(_) {}
    msgs.forEach(m => renderMsg(m)); // renderMsg dedupes by _seenIds
    chatLog.scrollTop = chatLog.scrollHeight;
    document.getElementById('load-more').style.display = msgs.length >= 10 ? '' : 'none';
  }).catch(()=>{});
}

function fetchMissed() {
  if (_lastTs === 0) return;
  fetch('/api/chat-history?since='+_lastTs+'&limit=100').then(r=>r.json()).then(d=>{
    const msgs = d.messages || [];
    if (!msgs.length) return;
    const atBottom = chatLog.scrollHeight - chatLog.scrollTop <= chatLog.clientHeight + 80;
    msgs.forEach(m => renderMsg(m));
    if (atBottom) chatLog.scrollTop = chatLog.scrollHeight;
  }).catch(()=>{});
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { fetchMissed(); if (!_sseConnected) connectSSE(); }
});

let _loadingMore = false;
function loadMore() {
  if (_loadingMore) return;
  _loadingMore = true;
  fetch('/api/chat-history?before='+_firstTs+'&limit=50').then(r=>r.json()).then(d=>{
    const msgs = (d.messages || []).reverse();
    const scrollBottom = chatLog.scrollHeight - chatLog.scrollTop;
    msgs.forEach(m => renderMsg(m));
    chatLog.scrollTop = chatLog.scrollHeight - scrollBottom;
    if ((d.messages||[]).length < 50) document.getElementById('load-more').style.display = 'none';
  }).catch(()=>{}).finally(()=>{ _loadingMore = false; });
}

chatLog.addEventListener('scroll', () => {
  if (chatLog.scrollTop < 80 && document.getElementById('load-more').style.display !== 'none') {
    loadMore();
  }
});

loadHistory();

// Update header title from identity file (runs unconditionally, separate from connectSSE)
fetch('/api/status').then(r=>r.json()).then(d=>{
  if (d.identity) { document.getElementById('header-title').textContent = d.identity; document.title = d.identity; }
}).catch(()=>{});

// Chat send
const _optimisticTexts = new Map(); // text → expiry timestamp
function sendChat() {
  const inp = document.getElementById('chat-in');
  const msg = inp.value.trim();
  if (!msg) return;
  inp.value = '';
  inp.style.height = '';
  // Optimistic render — show immediately
  const now = Date.now() / 1000;
  _optimisticTexts.set(msg, now + 10); // suppress SSE duplicate for 10s
  renderMsg({ts: now, dir: 'in', source: 'dashboard', sender: 'Ben (Dashboard)', text: msg});
  chatLog.scrollTop = chatLog.scrollHeight;
  fetch('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:msg, sender:'Ben (Dashboard)'})})
    .catch(e=>console.error(e));
}
const chatIn = document.getElementById('chat-in');
chatIn.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  if (e.key === 'Escape') { e.preventDefault(); chatIn.blur(); }
});
// Re-focus chat input when clicking empty space (not bubbles, buttons, inputs, etc.)
document.addEventListener('click', (e) => {
  if (!e.target.closest('button,a,input,select,textarea,.setting-row,.bubble-wrap,.bubble,.tab')) chatIn.focus();
});
chatIn.addEventListener('input', () => {
  chatIn.style.height = '';
  chatIn.style.height = Math.min(chatIn.scrollHeight, 120) + 'px';
});
function sendKey(key) {
  fetch('/api/tmux-key', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key})})
    .catch(e=>console.error(e));
}

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

function loadSecrets() {
  fetch('/api/secrets').then(r=>r.json()).then(d=>{
    const s = d.secrets || {};
    document.getElementById('sec-telegram-status').textContent = s.TELEGRAM_BOT_TOKEN ? '✓ set' : 'not set';
    document.getElementById('sec-telegram-status').style.color = s.TELEGRAM_BOT_TOKEN ? '#3fb950' : '#555';
    document.getElementById('sec-groq-status').textContent = s.GROQ_API_KEY ? '✓ set' : 'not set';
    document.getElementById('sec-groq-status').style.color = s.GROQ_API_KEY ? '#3fb950' : '#555';
  }).catch(()=>{});
}
function saveSecrets() {
  const data = {};
  const tg = document.getElementById('sec-telegram').value.trim();
  const groq = document.getElementById('sec-groq').value.trim();
  if (tg) data.TELEGRAM_BOT_TOKEN = tg;
  if (groq) data.GROQ_API_KEY = groq;
  if (!Object.keys(data).length) { alert('No values entered.'); return; }
  fetch('/api/secrets', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)})
    .then(r=>r.json()).then(d=>{
      if (d.ok) {
        document.getElementById('sec-telegram').value = '';
        document.getElementById('sec-groq').value = '';
        loadSecrets();
        alert('Secrets saved! Restart Clawdy for changes to take effect.');
      } else alert('Error: ' + d.error);
    });
}

// Cron management
function loadCrons() {
  fetch('/api/crons').then(r=>r.json()).then(d=>{
    const list = document.getElementById('crons-list');
    list.innerHTML = '';
    (d.crons || []).forEach(c => {
      const card = document.createElement('div');
      card.className = 'cron-card';
      card.dataset.name = c.name;
      const isHeartbeat = c.name === 'HEARTBEAT';
      card.innerHTML = `
        <h4>${c.name} <span class="cron-badge">${c.description || c.schedule}</span>
          ${isHeartbeat ? '' : `<button onclick="deleteCron('${c.name}')" style="margin-left:auto;background:#3a1a1a;color:#f66;font-size:11px;padding:2px 8px">Delete</button>`}
        </h4>
        <textarea id="cron-content-${c.name}">${esc(c.content)}</textarea>
        <div class="cron-footer">
          <input id="cron-sched-${c.name}" value="${c.schedule}" title="Cron schedule" />
          <button onclick="saveCron('${c.name}')">Save</button>
        </div>`;
      list.appendChild(card);
    });
  }).catch(()=>{});
}

function saveCron(name) {
  const content = document.getElementById('cron-content-'+name).value;
  const schedule = document.getElementById('cron-sched-'+name).value;
  fetch('/api/crons', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, content, schedule})
  }).then(r=>r.json()).then(d => {
    if (!d.ok) alert('Error: ' + d.error);
  });
}

function deleteCron(name) {
  if (!confirm('Delete cron ' + name + '?')) return;
  fetch('/api/crons/delete/' + name, {method:'POST'}).then(r=>r.json()).then(d => {
    if (d.ok) loadCrons(); else alert('Error: ' + d.error);
  });
}

function showAddCron() {
  document.getElementById('add-cron-form').style.display = '';
}

function addCron() {
  const name = document.getElementById('new-cron-name').value.trim().toUpperCase().replace(/\s+/g,'_');
  const schedule = document.getElementById('new-cron-schedule').value.trim();
  const content = document.getElementById('new-cron-content').value.trim();
  if (!name || !schedule || !content) { alert('Fill in all fields'); return; }
  fetch('/api/crons/add', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, schedule, content})
  }).then(r=>r.json()).then(d => {
    if (d.ok) {
      document.getElementById('add-cron-form').style.display = 'none';
      loadCrons();
    } else alert('Error: ' + d.error);
  });
}

// ── Push-to-talk ──────────────────────────────────────────────────────────────
let _pttRecorder = null, _pttChunks = [], _pttStream = null;

async function pttStart(e) {
  e.preventDefault();
  if (_pttRecorder) return;
  try {
    _pttStream = await navigator.mediaDevices.getUserMedia({audio: true});
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
    _pttRecorder = new MediaRecorder(_pttStream, {mimeType});
    _pttChunks = [];
    _pttRecorder.ondataavailable = e => { if (e.data.size > 0) _pttChunks.push(e.data); };
    _pttRecorder.start();
    document.getElementById('ptt-btn').classList.add('recording');
  } catch(err) {
    alert('Mic access denied: ' + err.message);
  }
}

async function pttStop(e) {
  e.preventDefault();
  if (!_pttRecorder) return;
  const recorder = _pttRecorder;
  _pttRecorder = null;
  await new Promise(resolve => { recorder.onstop = resolve; recorder.stop(); });
  _pttStream.getTracks().forEach(t => t.stop());
  _pttStream = null;
  document.getElementById('ptt-btn').classList.remove('recording');
  const blob = new Blob(_pttChunks, {type: recorder.mimeType});
  if (blob.size < 1000) return; // too short, ignore
  const input = document.getElementById('chat-in');
  const existing = input.value.trim();
  input.value = existing ? existing + ' …' : '…transcribing…';
  try {
    const resp = await fetch('/api/transcribe', {
      method: 'POST',
      headers: {'Content-Type': recorder.mimeType},
      body: blob
    });
    const d = await resp.json();
    if (d.ok && d.text) {
      input.value = existing ? existing + ' ' + d.text : d.text;
      input.focus();
      input.dispatchEvent(new Event('input'));
    } else {
      input.value = existing;
      alert('Transcription failed: ' + (d.error || 'unknown error'));
    }
  } catch(err) {
    input.value = existing;
    alert('Transcription error: ' + err.message);
  }
}

// ── PTT keyboard shortcut (hold Space when no input is focused) ───────────────
let _pttKeyDown = false;
document.addEventListener('keydown', e => {
  if (e.code !== 'Space') return;
  const tag = document.activeElement?.tagName;
  if (tag === 'TEXTAREA' || tag === 'INPUT') return;
  e.preventDefault();
  if (_pttKeyDown) return;
  _pttKeyDown = true;
  pttStart(e);
});
document.addEventListener('keyup', e => {
  if (e.code !== 'Space' || !_pttKeyDown) return;
  _pttKeyDown = false;
  pttStop(e);
});
</script>
</body>
</html>"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_env():
    """Load .env file into a dict."""
    env = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def log_chat_history(direction: str, sender: str, text: str, source: str = ""):
    """Append a message to the shared chat history file."""
    try:
        entry = json.dumps({"ts": time.time(), "dir": direction, "sender": sender,
                            "text": text, "source": source})
        with open(CHAT_HISTORY, "a") as f:
            f.write(entry + "\n")
    except Exception as e:
        log.debug(f"chat history write failed: {e}")


def inject_to_claude(text: str) -> bool:
    """Inject pre-formatted text into the tmux Claude session."""
    log.info(f"Injecting: {text[:80]}")
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}", text],
            check=True,
        )
        time.sleep(0.3)
        subprocess.run(
            ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}", "", "Enter"],
            check=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        log.error(f"tmux inject failed: {e}")
        return False


def enqueue_injection(text: str, sender: str, source: str = "dashboard"):
    """Send a message via the broker (channels system) or fall back to tmux injection.

    Posts to the local broker HTTP endpoint so messages arrive via the
    easyclaw-bridge MCP channel. Falls back to tmux inject if broker is down.
    """
    import urllib.request
    broker_port = int(os.environ.get("BROKER_PORT", "7899"))
    payload = json.dumps({
        "source": source,
        "sender": sender,
        "content": text,
    }).encode()

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{broker_port}/send",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        log.info("Message sent to broker: %s/%s: %s", source, sender, text[:60])
    except Exception as e:
        log.warning("Broker unavailable (%s), falling back to tmux inject", e)
        # Fall back to tmux injection
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        if text.startswith("/"):
            display = text
        elif source == "dashboard":
            display = f"[TELEGRAM from {sender} | {ts}]: {text}"
        elif source == "cron":
            display = f"[CRON | {ts}] {text}"
        else:
            display = text
        log_chat_history("in", sender, text, source=source)
        inject_to_claude(display)


# ── Whisper (optional — for push-to-talk transcription) ──────────────────────

_whisper_model = None


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
    """Transcribe audio file via Groq API (preferred) or local faster-whisper fallback."""
    env = load_env()
    groq_key = env.get("GROQ_API_KEY", "")
    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            with open(file_path, "rb") as f:
                result = client.audio.transcriptions.create(
                    file=(file_path.name, f, "audio/ogg"),
                    model="whisper-large-v3-turbo",
                    response_format="text",
                )
            text = result.strip() if isinstance(result, str) else (result.text or "").strip()
            log.info(f"Groq transcribed: {text[:80]!r}")
            if text:
                return text
        except Exception as e:
            log.warning(f"Groq transcription failed, falling back to local: {e}")
    # Local faster-whisper fallback
    model = get_whisper_model()
    if model is None:
        return None
    try:
        segments, info = model.transcribe(str(file_path), beam_size=5, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        log.info(f"Transcribed locally ({info.language}, {info.duration:.1f}s): {text[:80]!r}")
        if not text and info.language_probability < 0.7:
            segments, info = model.transcribe(str(file_path), beam_size=5, vad_filter=True, language="de")
            text = " ".join(seg.text.strip() for seg in segments).strip()
        return text if text else None
    except Exception as e:
        log.error(f"Transcription failed: {e}")
        return None


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    """Standalone dashboard HTTP handler."""

    def log_message(self, fmt, *args):
        log.debug(f"HTTP: {fmt % args}")

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/stream":
            self._serve_sse()
        elif self.path == "/api/services":
            self._serve_services()
        elif self.path == "/api/crons":
            self._serve_crons()
        elif self.path == "/api/settings":
            self._serve_settings()
        elif self.path == "/api/secrets":
            self._serve_secrets()
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

        if self.path == "/api/transcribe":
            self._handle_transcribe(body)
            return

        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        if self.path == "/chat":
            self._handle_chat(data)
        elif self.path == "/api/settings":
            self._update_settings(data)
        elif self.path == "/api/secrets":
            self._update_secrets(data)
        elif self.path == "/api/crons":
            self._update_cron(data)
        elif self.path == "/api/crons/add":
            self._add_cron(data)
        elif re.match(r"^/api/crons/delete/[A-Z0-9_]+$", self.path):
            self._delete_cron(self.path.split("/")[-1])
        elif re.match(r"^/api/restart/[a-zA-Z0-9\-\.@]+$", self.path):
            self._handle_restart()
        elif self.path == "/api/claude-start":
            self._handle_claude_start()
        elif self.path == "/api/tmux-key":
            self._handle_tmux_key(data)
        else:
            self.send_response(404)
            self.end_headers()

    # ── Dashboard chat ────────────────────────────────────────────────────

    def _handle_chat(self, data):
        msg = data.get("message", "").strip()
        sender = data.get("sender", "Ben (Dashboard)")
        source = data.get("source", "dashboard")
        direction = data.get("dir", "in")
        if source not in ("dashboard", "restart", "cron", "peer-out"):
            source = "dashboard"
        if not msg:
            self._json({"ok": False, "error": "empty"}, 400)
            return
        # Log-only sources (no tmux injection): restart, peer-out
        if source in ("restart", "peer-out"):
            log_chat_history(direction, sender, msg, source=source)
        else:
            enqueue_injection(msg, sender, source=source)
        self._json({"ok": True})

    def _handle_tmux_key(self, data):
        ALLOWED = {"Enter", "Up", "Down", "Left", "Right", "Escape", "Tab", "C-c"}
        key = data.get("key", "")
        if key not in ALLOWED:
            self._json({"ok": False, "error": "invalid key"}, 400)
            return
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}", "", key],
                check=True,
            )
            self._json({"ok": True})
        except subprocess.CalledProcessError as e:
            self._json({"ok": False, "error": str(e)}, 500)

    # ── Service restart ───────────────────────────────────────────────────

    def _handle_restart(self):
        svc = self.path.split("/api/restart/", 1)[-1]
        if svc in ("claude-code", "claude-code.service", "claude-code-channels", "claude-code-channels.service"):
            self._handle_claude_start()
            return
        try:
            subprocess.run(
                ["sudo", "systemctl", "restart", svc],
                check=True, timeout=15, capture_output=True,
            )
            self._json({"ok": True, "restarted": svc})
        except subprocess.CalledProcessError as e:
            self._json({"ok": False, "error": e.stderr.decode()[:200]}, 500)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _handle_claude_start(self):
        """Restart the Claude channels service."""
        try:
            r = subprocess.run(
                ["sudo", "systemctl", "restart", "claude-code-channels.service"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                self._json({"ok": True, "restarted": "claude-code-channels"})
                return
            error = r.stderr.strip() or r.stdout.strip() or "unknown error"
            self._json({"ok": False, "error": f"claude-code-channels.service failed: {error}"}, 500)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    # ── Transcribe ────────────────────────────────────────────────────────

    def _handle_transcribe(self, body: bytes):
        """Receive raw audio bytes from dashboard, transcribe, return text."""
        if not body:
            self._json({"ok": False, "error": "No audio data"}, 400)
            return
        import tempfile
        content_type = self.headers.get("Content-Type", "audio/webm")
        ext = ".ogg" if "ogg" in content_type else ".webm"
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(body)
                tmp_path = Path(tmp.name)
            text = transcribe_voice(tmp_path)
            tmp_path.unlink(missing_ok=True)
            if text:
                self._json({"ok": True, "text": text})
            else:
                self._json({"ok": False, "error": "Could not transcribe audio"})
        except Exception as e:
            log.error(f"Transcribe endpoint error: {e}")
            self._json({"ok": False, "error": str(e)}, 500)

    # ── Chat history ──────────────────────────────────────────────────────

    def _serve_chat_history(self):
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

    # ── HTML ──────────────────────────────────────────────────────────────

    def _serve_html(self):
        import hashlib
        try:
            identity = (EASYCLAW / "identity").read_text().strip().splitlines()[0]
        except Exception:
            identity = "Clawdy"
        html = DASHBOARD_HTML.replace(
            '<h1 id="header-title">Clawdy</h1>',
            f'<h1 id="header-title">{identity}</h1>',
        ).replace('<title>Clawdy</title>', f'<title>{identity}</title>')
        body = html.encode()
        etag = hashlib.md5(body).hexdigest()[:16]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    # ── Status ────────────────────────────────────────────────────────────

    def _get_status_data(self):
        try:
            r = subprocess.run(
                ["tmux", "has-session", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                capture_output=True, timeout=3,
            )
            alive = r.returncode == 0
        except Exception:
            alive = False
        try:
            status = STATUS_FILE.read_text().strip() if alive else "offline"
        except Exception:
            status = "idle" if alive else "offline"
        try:
            identity = (EASYCLAW / "identity").read_text().strip().splitlines()[0]
        except Exception:
            identity = "Clawdy"
        return {"alive": alive, "status": status, "queue_depth": 0, "identity": identity}

    def _serve_status(self):
        self._json(self._get_status_data())

    # ── SSE stream ────────────────────────────────────────────────────────

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
        last_client_count = -1
        last_typing = False
        last_chat_ts = 0.0
        try:
            if CHAT_HISTORY.exists():
                for line in CHAT_HISTORY.read_text().splitlines():
                    if line.strip():
                        try:
                            last_chat_ts = max(last_chat_ts, json.loads(line).get("ts", 0))
                        except Exception:
                            pass
        except Exception:
            pass
        tick = 0
        try:
            # Send initial status immediately
            st = self._get_status_data()
            push("status", st)
            last_status = st

            while True:
                # Terminal (every tick = 0.5s) — capture last 500 lines of scrollback + visible pane
                r = subprocess.run(
                    ["tmux", "capture-pane", "-pt", f"{TMUX_SESSION}:{TMUX_WINDOW}", "-S", "-500"],
                    capture_output=True, text=True, timeout=5,
                )
                raw = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^[Oc]', '', r.stdout)
                term_content = "\n".join(line.rstrip() for line in raw.splitlines()).strip()
                if term_content != last_term:
                    push("terminal", {"content": term_content})
                    last_term = term_content

                # Detect if Claude is actively working — "esc to interrupt" in the visible pane
                try:
                    visible_r = subprocess.run(
                        ["tmux", "capture-pane", "-pt", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                        capture_output=True, text=True, timeout=3,
                    )
                    visible = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[^[Oc]', '', visible_r.stdout)
                    last3 = "\n".join(visible.splitlines()[-3:])
                    is_typing = bool(re.search(r'esc to interrupt', last3, re.IGNORECASE))
                except Exception:
                    is_typing = False
                if is_typing != last_typing:
                    push("typing", {"active": is_typing})
                    last_typing = is_typing

                # Status (every 6 ticks = 3s)
                if tick % 6 == 0:
                    st = self._get_status_data()
                    if st != last_status:
                        push("status", st)
                        last_status = st
                    # Auto-resize based on attached clients
                    try:
                        cl = subprocess.run(
                            ["tmux", "list-clients", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                            capture_output=True, text=True, timeout=3,
                        )
                        client_count = len([l for l in cl.stdout.splitlines() if l.strip()])
                        if client_count == 0 and last_client_count != 0:
                            subprocess.run(
                                ["tmux", "resize-window", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}", "-x", "220", "-y", "50"],
                                capture_output=True, timeout=3,
                            )
                        elif client_count > 0 and last_client_count == 0:
                            subprocess.run(
                                ["tmux", "resize-window", "-A", "-t", f"{TMUX_SESSION}:{TMUX_WINDOW}"],
                                capture_output=True, timeout=3,
                            )
                        last_client_count = client_count
                    except Exception:
                        pass

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

    # ── Services ──────────────────────────────────────────────────────────

    def _serve_services(self):
        # Core systemd services to always check
        systemd_services = [
            "claude-code-channels.service",
            "claude-code.service",      # legacy, may be disabled
            "clawdy-bridge.service",    # legacy, may be disabled
        ]
        # Scan for additional loaded services matching known patterns
        try:
            r = subprocess.run(
                ["systemctl", "list-units", "--no-legend", "--no-pager",
                 "-t", "service", "--state=loaded"],
                capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.splitlines():
                parts = line.split()
                if parts:
                    name = parts[0]
                    if any(x in name for x in ("meme-scanner", "fomofollow", "clawdy-", "easyclaw-", "brainrot")):
                        if name not in systemd_services:
                            systemd_services.append(name)
        except Exception:
            pass

        statuses = []
        for svc in systemd_services:
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5,
                )
                statuses.append({"name": svc, "active": r.stdout.strip() == "active"})
            except Exception:
                statuses.append({"name": svc, "active": False})

        # Process-based services (broker and dashboard run as plain processes)
        process_checks = [
            ("broker (process)", "broker.py"),
            ("dashboard (process)", "dashboard.py"),
        ]
        try:
            ps = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5,
            )
            ps_lines = ps.stdout.splitlines()
            for display_name, script_name in process_checks:
                pid = None
                for line in ps_lines:
                    if script_name in line and "python" in line:
                        parts = line.split()
                        if len(parts) > 1:
                            try:
                                pid = int(parts[1])
                            except ValueError:
                                pass
                        break
                entry = {"name": display_name, "active": pid is not None}
                if pid is not None:
                    entry["pid"] = pid
                statuses.append(entry)
        except Exception:
            for display_name, _ in process_checks:
                statuses.append({"name": display_name, "active": False})

        self._json({"services": statuses})

    # ── Crons ─────────────────────────────────────────────────────────────

    def _crons_dir(self):
        return EASYCLAW / "workspace" / "crons"

    def _crons_meta(self):
        meta_file = self._crons_dir() / "crons.json"
        try:
            return json.loads(meta_file.read_text())
        except Exception:
            return []

    def _save_crons_meta(self, meta):
        meta_file = self._crons_dir() / "crons.json"
        meta_file.write_text(json.dumps(meta, indent=2))

    def _serve_crons(self):
        crons_dir = self._crons_dir()
        meta = self._crons_meta()
        result = []
        for entry in meta:
            name = entry.get("name", "")
            md_file = crons_dir / f"{name}.md"
            content = md_file.read_text() if md_file.exists() else ""
            result.append({
                "name": name,
                "schedule": entry.get("schedule", ""),
                "enabled": entry.get("enabled", True),
                "description": entry.get("description", ""),
                "content": content,
            })
        self._json({"crons": result})

    def _update_cron(self, data):
        name = data.get("name", "").upper().replace(" ", "_")
        if not name:
            self._json({"ok": False, "error": "name required"}, 400)
            return
        crons_dir = self._crons_dir()
        meta = self._crons_meta()
        entry = next((e for e in meta if e["name"] == name), None)
        if not entry:
            self._json({"ok": False, "error": "cron not found"}, 404)
            return
        if "content" in data:
            (crons_dir / f"{name}.md").write_text(data["content"])
        if "schedule" in data:
            entry["schedule"] = data["schedule"]
            self._sync_crontab(name, entry["schedule"], entry.get("enabled", True))
        if "enabled" in data:
            entry["enabled"] = data["enabled"]
            self._sync_crontab(name, entry["schedule"], entry["enabled"])
        self._save_crons_meta(meta)
        self._json({"ok": True})

    def _add_cron(self, data):
        name = data.get("name", "").upper().strip().replace(" ", "_")
        schedule = data.get("schedule", "0 9 * * 1")
        content = data.get("content", "")
        description = data.get("description", "")
        if not name:
            self._json({"ok": False, "error": "name required"}, 400)
            return
        crons_dir = self._crons_dir()
        meta = self._crons_meta()
        if any(e["name"] == name for e in meta):
            self._json({"ok": False, "error": "cron already exists"}, 400)
            return
        (crons_dir / f"{name}.md").write_text(content)
        meta.append({"name": name, "schedule": schedule, "enabled": True, "description": description})
        self._save_crons_meta(meta)
        self._sync_crontab(name, schedule, True)
        self._json({"ok": True})

    def _delete_cron(self, name):
        if name == "HEARTBEAT":
            self._json({"ok": False, "error": "HEARTBEAT cannot be deleted"})
            return
        meta = self._crons_meta()
        meta = [e for e in meta if e["name"] != name]
        self._save_crons_meta(meta)
        self._sync_crontab(name, "", False)
        md_file = self._crons_dir() / f"{name}.md"
        if md_file.exists():
            md_file.unlink()
        self._json({"ok": True})

    def _sync_crontab(self, name, schedule, enabled):
        """Add, update or remove a cron entry for the given cron name."""
        runner = str(EASYCLAW / "scripts" / "clawdy-cron-runner.sh")
        cron_log = str(EASYCLAW / f"cron-{name.lower()}.log")
        new_line = f"{schedule} {runner} {name} >> {cron_log} 2>&1"
        try:
            r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            lines = [l for l in r.stdout.splitlines() if f"{runner} {name}" not in l]
            if enabled and schedule:
                lines.append(new_line)
            subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)
        except Exception as e:
            log.warning(f"crontab sync failed: {e}")

    # ── Settings ──────────────────────────────────────────────────────────

    def _serve_settings(self):
        hidden = {"TELEGRAM_BOT_TOKEN", "BRIDGE_API_KEY", "GROQ_API_KEY"}
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
            _model_raw = cs.get("model", "sonnet")
            _model_map = {
                "claude-opus-4-6": "opus",
                "claude-sonnet-4-6": "sonnet",
                "claude-haiku-4-5-20251001": "haiku",
            }
            settings["claude_model"] = _model_map.get(_model_raw, _model_raw)
            settings["claude_effort"] = cs.get("effortLevel", "medium")
        except Exception:
            pass
        self._json({"settings": settings})

    def _update_settings(self, data):
        updated = []
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

    # ── Secrets ───────────────────────────────────────────────────────────

    def _serve_secrets(self):
        """Return which secret keys are set (boolean only, never the values)."""
        secret_keys = ["TELEGRAM_BOT_TOKEN", "BRIDGE_API_KEY", "GROQ_API_KEY"]
        result = {}
        try:
            env_text = ENV_FILE.read_text()
            for k in secret_keys:
                match = re.search(f"^{k}=(.+)$", env_text, re.MULTILINE)
                result[k] = bool(match and match.group(1).strip())
        except Exception:
            result = {k: False for k in secret_keys}
        self._json({"secrets": result})

    def _update_secrets(self, data):
        """Write secret keys to .env (only if non-empty value provided)."""
        secret_keys = {"TELEGRAM_BOT_TOKEN", "BRIDGE_API_KEY", "GROQ_API_KEY"}
        updated = []
        try:
            env_text = ENV_FILE.read_text()
            for k, v in data.items():
                if k not in secret_keys or not v.strip():
                    continue
                if re.search(f"^{k}=", env_text, re.MULTILINE):
                    env_text = re.sub(f"^{k}=.*$", f"{k}={v.strip()}", env_text, flags=re.MULTILINE)
                else:
                    env_text += f"\n{k}={v.strip()}"
                updated.append(k)
            ENV_FILE.write_text(env_text)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": True, "updated": updated})

    # ── Activity ──────────────────────────────────────────────────────────

    def _serve_activity(self):
        try:
            lines = ACTIVITY_LOG.read_text().splitlines()
            recent = "\n".join(lines[-100:])
        except Exception:
            recent = ""
        self._json({"activity": recent})

    # ── JSON response helper ──────────────────────────────────────────────

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    port = int(os.environ.get("DASHBOARD_PORT", "8766"))
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")

    log.info(f"Clawdy Standalone Dashboard starting on {host}:{port}")

    # Set up TLS if certs exist
    server = ThreadingHTTPServer((host, port), DashboardHandler)

    tls_cert = EASYCLAW / "tls.crt"
    tls_key = EASYCLAW / "tls.key"
    if tls_cert.exists() and tls_key.exists():
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        log.info(f"TLS enabled — serving HTTPS on {host}:{port}")
    else:
        log.info(f"No TLS certs found — serving HTTP on {host}:{port}")

    try:
        log.info(f"Dashboard available at http://{host}:{port}")
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Dashboard shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
