#!/bin/bash
# update.sh — pull latest easyclaw and sync workspace to ~/.easyclaw/
#
# Run from the repo dir or any path:
#   bash /path/to/easyclaw/update.sh
#
# What it does:
#   1. git pull latest changes
#   2. Copy workspace/scripts/* → ~/.easyclaw/scripts/
#   3. Regenerate ~/claude-start.sh from template
#   4. Update clawdy-restart + clawdy-update in /usr/local/bin/ if changed
#   5. Re-register clawdy-mcp in ~/.claude.json (ensures correct path)
#   6. Update systemd service files if changed
#   7. Restart telegram-bot service
#   8. Restart clawdy (via clawdy-restart) so MCP + script changes take effect

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EASYCLAW="$HOME/.easyclaw"
SCRIPTS="$EASYCLAW/scripts"

echo "╔══════════════════════════════════════╗"
echo "║         EasyClaw Updater             ║"
echo "╚══════════════════════════════════════╝"
echo

# ── 1. Pull latest ────────────────────────────────────────────────────────
echo "➜ Pulling latest changes..."
git -C "$REPO" checkout main
git -C "$REPO" pull
echo

# ── 2. Sync workspace/scripts/ → ~/.easyclaw/scripts/ ────────────────────
echo "➜ Syncing scripts to $SCRIPTS..."
mkdir -p "$SCRIPTS"
cp "$REPO/workspace/scripts/"* "$SCRIPTS/"
chmod +x "$SCRIPTS/"*.sh "$SCRIPTS/"*.py 2>/dev/null || true
echo "  Copied: $(ls "$REPO/workspace/scripts/" | tr '\n' ' ')"
echo

# ── 3. Regenerate ~/claude-start.sh ──────────────────────────────────────
echo "➜ Regenerating ~/claude-start.sh from template..."
sed "s|%%HOME%%|$HOME|g" "$REPO/claude-start.sh.template" > "$HOME/claude-start.sh"
chmod +x "$HOME/claude-start.sh"
echo "  Written: $HOME/claude-start.sh"
echo

# ── 4. Update clawdy-restart and clawdy-update in /usr/local/bin/ ────────
for bin_script in clawdy-restart update.sh; do
    dest_name="${bin_script/update.sh/clawdy-update}"  # rename update.sh → clawdy-update
    src="$REPO/$bin_script"
    dest="/usr/local/bin/$dest_name"
    [ -f "$src" ] || continue
    if ! diff -q "$src" "$dest" &>/dev/null 2>&1; then
        echo "➜ Updating $dest_name..."
        sudo cp "$src" "$dest"
        sudo chmod +x "$dest"
        echo "  Updated: $dest"
    else
        echo "➜ $dest_name unchanged — skipping"
    fi
done
echo

# ── 5. Ensure clawdy-mcp path is correct in ~/.claude.json ───────────────
echo "➜ Ensuring clawdy-mcp is registered in ~/.claude.json..."
CLAUDE_JSON="$HOME/.claude.json"
MCP_PATH="$SCRIPTS/clawdy-mcp.py"
[ -f "$CLAUDE_JSON" ] || echo '{}' > "$CLAUDE_JSON"

if grep -q "clawdy-mcp\.py" "$CLAUDE_JSON"; then
    # Replace any stale path — grep every line containing clawdy-mcp.py and swap it
    sed -i "s|\"[^\"]*clawdy-mcp\.py\"|\"$MCP_PATH\"|g" "$CLAUDE_JSON"
    echo "  Updated path → $MCP_PATH"
else
    # No registration found — create it
    jq --arg home "$HOME" --arg mcp "$MCP_PATH" \
        '.projects[$home].mcpServers["clawdy-mcp"] = {
            "type": "stdio",
            "command": "python3",
            "args": [$mcp],
            "env": {}
        }' "$CLAUDE_JSON" > "$CLAUDE_JSON.tmp" && mv "$CLAUDE_JSON.tmp" "$CLAUDE_JSON"
    echo "  Created registration → $MCP_PATH"
fi
echo

# ── 7. Update systemd service files if changed ───────────────────────────
RELOAD_SERVICES=0
for svc_template in "$REPO/services/"*.service; do
    [ -f "$svc_template" ] || continue
    svc_name=$(basename "$svc_template")
    installed="/etc/systemd/system/$svc_name"
    tmp=$(mktemp)

    sed \
        -e "s|%%USER%%|$USER|g" \
        -e "s|%%HOME%%|$HOME|g" \
        "$svc_template" > "$tmp"

    if [ ! -f "$installed" ] || ! diff -q "$tmp" "$installed" &>/dev/null; then
        echo "➜ Updating $svc_name..."
        sudo cp "$tmp" "$installed"
        sudo chmod 644 "$installed"
        RELOAD_SERVICES=1
        echo "  Updated: $installed"
    else
        echo "➜ $svc_name unchanged — skipping"
    fi
    rm -f "$tmp"
done

if [ "$RELOAD_SERVICES" = "1" ]; then
    echo "➜ Reloading systemd daemon..."
    sudo systemctl daemon-reload
fi
echo

# ── 8. Restart telegram-bot ───────────────────────────────────────────────
echo "➜ Restarting telegram-bot..."
if sudo systemctl restart clawdy-telegram-bot.service 2>/dev/null; then
    echo "  Restarted: clawdy-telegram-bot.service"
else
    echo "  (telegram-bot not active — skipping)"
fi
echo

# ── 9. Restart clawdy (MCP + script changes need a fresh session) ─────────
echo "➜ Restarting Clawdy..."
clawdy-restart "update.sh ran — new code deployed" "Update complete — new code is live. Continue where you left off."
echo

echo "✓ Update complete!"
