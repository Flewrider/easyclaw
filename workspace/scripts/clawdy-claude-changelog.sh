#!/bin/bash
# Weekly: check Claude Code changelog and inject into Claude session if new version found.
SEEN_FILE="$HOME/.easyclaw/claude-changelog-seen"
ENV_FILE="$HOME/.easyclaw/.env"

BRIDGE_PORT=$(grep "^BRIDGE_PORT=" "$ENV_FILE" 2>/dev/null | cut -d= -f2)
BRIDGE_PORT="${BRIDGE_PORT:-8765}"

# Fetch + decode changelog
CHANGELOG=$(curl -sf \
  -H "Accept: application/vnd.github.v3+json" \
  "https://api.github.com/repos/anthropics/claude-code/contents/CHANGELOG.md" \
  | python3 -c "import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)['content']).decode())")

if [ -z "$CHANGELOG" ]; then
  echo "Fetch failed" >&2; exit 1
fi

LATEST=$(echo "$CHANGELOG" | grep -m1 '^## ' | sed 's/## //')
LAST_SEEN=$(cat "$SEEN_FILE" 2>/dev/null || true)

if [ "$LATEST" = "$LAST_SEEN" ]; then
  echo "No new version ($LATEST)"; exit 0
fi

# Extract the new version's changelog section
SECTION=$(echo "$CHANGELOG" | awk '/^## /{n++} n==1 && !/^## /' | tail -n+2 | awk '/^## /{exit} 1' | grep "^- " | head -30)

MSG="[CRON - CLAUDE CODE CHANGELOG] New Claude Code version detected: $LATEST (was: ${LAST_SEEN:-unknown}). Read the following changelog and send Ben a Telegram summary of anything interesting or relevant to our setup (voice, MCP, tools, dashboard, agents, performance). Changelog:

$SECTION"

PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'message':sys.argv[1],'source':'cron','sender':'cron'}))" "$MSG")
curl -sf --max-time 5 -X POST "http://127.0.0.1:$BRIDGE_PORT/chat" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD" > /dev/null

echo "$LATEST" > "$SEEN_FILE"
echo "Injected changelog for $LATEST"
