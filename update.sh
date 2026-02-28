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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# When installed as /usr/local/bin/clawdy-update, SCRIPT_DIR is /usr/local/bin (not a git repo).
# Fall back to the stored repo path written by the previous update run.
if git -C "$SCRIPT_DIR" rev-parse --git-dir &>/dev/null 2>&1; then
    REPO="$SCRIPT_DIR"
elif [ -f "$HOME/.easyclaw/repo-path" ]; then
    REPO="$(cat "$HOME/.easyclaw/repo-path")"
else
    echo "Error: Cannot find easyclaw repo. Run update.sh directly from the repo first."
    exit 1
fi
EASYCLAW="$HOME/.easyclaw"
SCRIPTS="$EASYCLAW/scripts"

echo "╔══════════════════════════════════════╗"
echo "║         EasyClaw Updater             ║"
echo "╚══════════════════════════════════════╝"
echo

# ── 1. Pull latest + self-update ──────────────────────────────────────────
# Skip pull if we already re-exec'd after a pull (avoids infinite loop)
if [ "${_CLAWDY_UPDATED:-}" != "1" ]; then
    echo "➜ Pulling latest changes..."
    git -C "$REPO" checkout main
    git -C "$REPO" pull
    # Store repo path so clawdy-update (in /usr/local/bin) can find it next run
    echo "$REPO" > "$HOME/.easyclaw/repo-path"
    # Install the freshly-pulled update.sh to /usr/local/bin NOW, then re-exec
    # so the rest of the update runs with the new version of this script.
    sudo cp "$REPO/update.sh" /usr/local/bin/clawdy-update
    sudo chmod +x /usr/local/bin/clawdy-update
    echo "  Self-updated clawdy-update — re-executing with new version..."
    echo
    export _CLAWDY_UPDATED=1
    exec /usr/local/bin/clawdy-update "$@"
fi
echo "  (already pulled — continuing with updated script)"
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

# ── 4. Update clawdy-restart in /usr/local/bin/ ──────────────────────────
# (clawdy-update already self-updated in step 1)
src="$REPO/clawdy-restart"
if [ -f "$src" ]; then
    sudo cp "$src" /usr/local/bin/clawdy-restart
    sudo chmod +x /usr/local/bin/clawdy-restart
    echo "➜ Installed clawdy-restart → /usr/local/bin/clawdy-restart"
fi
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
# Kill any stray telegram-bot.py processes not managed by systemd
# (can happen after manual starts or workspace-path transitions)
pkill -f "telegram-bot.py" 2>/dev/null && echo "  Killed stray telegram-bot.py processes" || true
sleep 1
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
