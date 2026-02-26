#!/bin/bash
# EasyClaw Setup Script
# Deploys Claude Code + Telegram bot on a VPS with full automation
# Run with: bash setup.sh [--verbose]

set -euo pipefail

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Global config (declared early so accessible in all functions)
SETUP_USER=""
CLAUDE_MODEL=""
TELEGRAM_TOKEN=""
SESSION_ID=""
BOT_NAME=""
BOT_PURPOSE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERBOSE="${VERBOSE:-0}"
LOG_FILE="/tmp/easyclaw-setup-$(date +%s).log"
STATE_FILE="$HOME/.easyclaw-setup-state"
COMPLETED_STEPS=()

# Parse --verbose flag
if [[ " $* " =~ " --verbose " ]]; then
    VERBOSE=1
    set -- "${@/--verbose/}"
fi

# Helper: Log to file and optionally print
log() {
    local level="$1"
    shift
    local msg="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] $msg" >> "$LOG_FILE"
    if [ "$VERBOSE" = "1" ]; then
        echo "[$level] $msg" >&2
    fi
}

# Helper: Print colored messages (with logging)
print_info() { echo -e "${GREEN}➜${NC} $*"; log "INFO" "$*"; }
print_warn() { echo -e "${YELLOW}⚠${NC} $*"; log "WARN" "$*"; }
print_error() { echo -e "${RED}✗${NC} $*"; log "ERROR" "$*"; }
print_success() { echo -e "${GREEN}✓${NC} $*"; log "SUCCESS" "$*"; }

# Helper: Confirm with user (returns exit code)
confirm() {
    local prompt="$1"
    local response
    read -p "$(echo -e "${YELLOW}?${NC} $prompt (y/n): ")" -n 1 -r response
    echo
    log "PROMPT" "User response to '$prompt': $response"
    [[ $response =~ ^[Yy]$ ]]
}

# ── State persistence ─────────────────────────────────────────────────────────

save_state() {
    cat > "$STATE_FILE" << EOF
SETUP_USER="$SETUP_USER"
CLAUDE_MODEL="$CLAUDE_MODEL"
TELEGRAM_TOKEN="$TELEGRAM_TOKEN"
SESSION_ID="$SESSION_ID"
BOT_NAME="$BOT_NAME"
BOT_PURPOSE="$BOT_PURPOSE"
COMPLETED_STEPS=(${COMPLETED_STEPS[*]+"${COMPLETED_STEPS[*]}"})
EOF
    log "DEBUG" "State saved to $STATE_FILE"
}

load_state() {
    # shellcheck source=/dev/null
    source "$STATE_FILE"
    log "INFO" "Loaded saved state from $STATE_FILE"
}

mark_done() {
    COMPLETED_STEPS+=("$1")
    save_state
}

is_done() {
    local step="$1"
    for s in "${COMPLETED_STEPS[@]+"${COMPLETED_STEPS[@]}"}"; do
        [ "$s" = "$step" ] && return 0
    done
    return 1
}

# Ask user to resume or restart if a previous state exists
handle_resume() {
    [ ! -f "$STATE_FILE" ] && return 0

    local done_list
    done_list=$(source "$STATE_FILE" 2>/dev/null && echo "${COMPLETED_STEPS[*]+"${COMPLETED_STEPS[*]}"}" || echo "")

    echo
    echo -e "${YELLOW}⚡ Previous setup found.${NC}"
    [ -n "$done_list" ] && echo "   Completed steps: $done_list"
    echo

    if confirm "Resume from where you left off? (n = restart from scratch)"; then
        load_state
        print_info "Resuming setup..."
        log "INFO" "User chose to resume setup"
    else
        rm -f "$STATE_FILE"
        print_info "Starting fresh..."
        log "INFO" "User chose to restart setup"
    fi
}

# ── Step 1: Check dependencies ─────────────────────────────────────────────────
check_dependencies() {
    print_info "Checking dependencies..."
    log "DEBUG" "Starting dependency check"
    local missing=()
    local checks=(
        "curl:curl"
        "jq:jq"
        "git:git"
        "python3:python3"
        "tmux:tmux"
    )

    local apt_missing=()
    for check in "${checks[@]}"; do
        IFS=: read -r cmd pkg <<< "$check"
        if ! command -v "$cmd" &> /dev/null; then
            apt_missing+=("$pkg")
            log "WARN" "Missing: $pkg"
        else
            log "DEBUG" "Found: $cmd"
        fi
    done

    # Auto-install missing apt packages
    if [ ${#apt_missing[@]} -gt 0 ]; then
        print_info "Auto-installing missing packages: ${apt_missing[*]}"
        log "INFO" "Auto-installing: ${apt_missing[*]}"
        local install_cmd="apt-get install -y"
        [ "$(id -u)" != "0" ] && install_cmd="sudo apt-get install -y"
        apt-get update -qq 2>/dev/null || true
        if $install_cmd "${apt_missing[@]}" 2>&1 | tee -a "$LOG_FILE"; then
            print_success "Packages installed: ${apt_missing[*]}"
            log "INFO" "Packages installed successfully"
        else
            for pkg in "${apt_missing[@]}"; do
                missing+=("$pkg")
            done
        fi
    fi

    # Ensure ~/.local/bin is in PATH before checking (installer puts claude there)
    export PATH="$HOME/.local/bin:$PATH"

    # Check Claude CLI — install automatically if missing
    if ! command -v claude &> /dev/null && [ ! -f "$HOME/.local/bin/claude" ]; then
        print_info "Claude Code CLI not found — installing..."
        log "INFO" "Installing Claude Code CLI via official installer"
        if curl -fsSL https://claude.ai/install.sh | bash; then
            print_success "Claude Code CLI installed"
            log "INFO" "Claude Code CLI installed successfully"
            # Add ~/.local/bin to PATH permanently and reload for this session
            if ! grep -q 'local/bin' ~/.bashrc; then
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
            fi
            export PATH="$HOME/.local/bin:$PATH"
        else
            missing+=("claude (Claude Code CLI) — install failed, retry manually: curl -fsSL https://claude.ai/install.sh | bash")
            log "ERROR" "Claude Code CLI install failed"
        fi
    else
        log "DEBUG" "Found: claude CLI ($(claude --version 2>/dev/null | head -1))"
    fi

    # Check Python version if present
    if command -v python3 &> /dev/null; then
        py_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
        log "DEBUG" "Python version: $py_version"
        if [[ $(printf '%s\n' "3.8" "$py_version" | sort -V | head -n 1) != "3.8" ]]; then
            missing+=("python3 (>=3.8, found $py_version)")
            log "ERROR" "Python version too old: $py_version"
        fi
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        print_error "Could not install required dependencies:"
        for item in "${missing[@]}"; do
            echo "  - $item"
            log "ERROR" "Missing dependency: $item"
        done
        return 1
    fi

    # Check sudo access — required for systemctl, tee, ufw, etc.
    check_sudo_access

    print_success "All dependencies present"
}

# Step 0b: Ensure the current user can use sudo
check_sudo_access() {
    log "DEBUG" "Checking sudo access for user: $USER"
    # Check whether the user is in the sudo group or wheel group
    if groups "$USER" 2>/dev/null | grep -qE '\b(sudo|wheel|admin)\b'; then
        print_success "User $USER has sudo access"
        log "INFO" "User $USER is in sudoers group"
        return 0
    fi

    # Also try a quick sudo -n check (works if NOPASSWD is configured)
    if sudo -n true 2>/dev/null; then
        print_success "User $USER has sudo access (NOPASSWD)"
        log "INFO" "User $USER has passwordless sudo configured"
        return 0
    fi

    log "WARN" "User $USER does NOT have sudo access"
    print_warn "User '$USER' is NOT in the sudoers group."
    echo
    echo "  This script uses sudo for: systemctl, tee, ufw, and package installs."
    echo "  To add yourself to the sudo group, run the following AS ROOT:"
    echo
    echo "    ${YELLOW}sudo usermod -aG sudo $USER${NC}"
    echo
    echo "  Then log out and back in for the change to take effect."
    echo
    if confirm "Continue anyway (commands requiring sudo will fail)?"; then
        print_warn "Continuing without confirmed sudo access — some steps may fail"
        log "WARN" "User chose to continue without sudo access"
        return 0
    else
        print_error "Aborting. Add $USER to sudoers and re-run setup."
        log "ERROR" "Aborted: user lacks sudo access"
        return 1
    fi
}

# Step 2: Initialize session ID
init_session_id() {
    print_info "Initializing Claude Code session..."
    log "DEBUG" "Session file location: $HOME/.claude/session-id"
    local session_file="$HOME/.claude/session-id"

    if [ -f "$session_file" ]; then
        SESSION_ID=$(cat "$session_file")
        print_success "Reusing existing session ID: $SESSION_ID"
        log "INFO" "Reusing session ID: $SESSION_ID (from $session_file)"
    else
        SESSION_ID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
        log "DEBUG" "Generated new session ID: $SESSION_ID"
        mkdir -p "$HOME/.claude"
        echo "$SESSION_ID" > "$session_file"
        chmod 600 "$session_file"
        print_success "Created new session ID: $SESSION_ID"
        log "INFO" "Wrote session ID to $session_file"
    fi
}

# Step 3: First-launch Claude setup — OAuth + session init
first_launch_claude() {
    if [ -f "$HOME/.claude/claude.json" ]; then
        print_success "Claude Code already initialized"
        return 0
    fi

    print_info "Starting Claude Code initial setup (OAuth login)..."
    echo
    echo -e "  ${YELLOW}A browser window will open for Claude OAuth login.${NC}"
    echo    "  Complete the login, then Claude will initialize and exit automatically."
    echo

    # Run directly — passes 'exit' as the first prompt so Claude exits after OAuth completes
    claude --session-id "$SESSION_ID" --dangerously-skip-permissions --browser exit

    if [ ! -f "$HOME/.claude/claude.json" ]; then
        print_warn "claude.json not found after setup — OAuth may not have completed."
        if ! confirm "Continue anyway?"; then
            return 1
        fi
    else
        print_success "Claude Code initialized"
        log "INFO" "claude.json created — OAuth complete"
    fi
}

# Step 4: Patch claude.json to trust home directory (workaround for Claude Code bug)
patch_claude_json() {
    print_info "Configuring Claude Code settings..."
    local claude_json="$HOME/.claude/claude.json"

    if [ ! -f "$claude_json" ]; then
        print_warn "claude.json not found, creating minimal config"
        echo '{}' > "$claude_json"
    fi

    # Check if trusted_paths already exists and contains our home
    if jq -e ".trusted_paths | index(\"$HOME\")" "$claude_json" &> /dev/null; then
        print_success "Home directory already trusted in Claude Code"
        return 0
    fi

    # Add home to trusted_paths
    jq ".trusted_paths |= . + [\"$HOME\"]" "$claude_json" > "$claude_json.tmp"
    mv "$claude_json.tmp" "$claude_json"
    print_success "Added home directory to trusted paths"
}

# Step 5: Install CLAUDE.md — full operational instructions for the agent
install_bot_identity() {
    local src="$SCRIPT_DIR/bot-identity.md"
    local dest_claude_md="$HOME/CLAUDE.md"

    if [ ! -f "$src" ]; then
        print_warn "bot-identity.md not found in $SCRIPT_DIR — skipping CLAUDE.md setup"
        return 0
    fi

    print_info "Installing CLAUDE.md (agent instructions)..."
    log "DEBUG" "Source: $src -> $dest_claude_md"

    # Substitute placeholders
    local tmp
    tmp=$(mktemp)
    sed \
        -e "s|%%BOT_NAME%%|${BOT_NAME:-Clawdy}|g" \
        -e "s|%%BOT_PURPOSE%%|${BOT_PURPOSE:-your personal AI assistant}|g" \
        -e "s|%%SETUP_USER%%|${SETUP_USER:-$USER}|g" \
        "$src" > "$tmp"

    # Write as ~/CLAUDE.md — this is the complete operational config for the agent
    if [ -f "$dest_claude_md" ]; then
        print_warn "CLAUDE.md already exists — overwriting with EasyClaw template"
        log "WARN" "Overwriting existing CLAUDE.md"
    fi

    cp "$tmp" "$dest_claude_md"
    rm -f "$tmp"
    print_success "Installed CLAUDE.md -> $dest_claude_md"
    log "INFO" "CLAUDE.md installed with bot name: ${BOT_NAME:-Clawdy}, user: ${SETUP_USER:-$USER}"
}

# Step 5b: Collect configuration from user
collect_config() {
    print_info "Configuring EasyClaw..."

    # Username
    local default_user="${USER:-ubuntu}"
    read -p "$(echo -e "${YELLOW}?${NC} System username (default: $default_user): ")" username
    username="${username:-$default_user}"

    # Validate username exists
    if ! id "$username" &> /dev/null; then
        print_error "User $username does not exist"
        return 1
    fi
    SETUP_USER="$username"

    # Telegram token (optional)
    read -p "$(echo -e "${YELLOW}?${NC} Telegram bot token (optional, press enter to skip): ")" token
    if [ -n "$token" ]; then
        # Basic validation: should be digits:AlphaNum
        if [[ ! $token =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            print_warn "Token format looks incorrect (should be: 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11)"
            if confirm "Use anyway?"; then
                TELEGRAM_TOKEN="$token"
            fi
        else
            TELEGRAM_TOKEN="$token"
        fi
    fi

    # Claude model
    echo
    print_info "Default Claude model:"
    echo "  1) Haiku (fast, cheap) - RECOMMENDED"
    echo "  2) Sonnet (balanced)"
    echo "  3) Opus (most capable)"
    read -p "$(echo -e "${YELLOW}?${NC} Choose (1-3, default 1): ")" model_choice
    model_choice="${model_choice:-1}"

    case "$model_choice" in
        1) CLAUDE_MODEL="haiku" ;;
        2) CLAUDE_MODEL="sonnet" ;;
        3) CLAUDE_MODEL="opus" ;;
        *) print_warn "Invalid choice, using haiku"; CLAUDE_MODEL="haiku" ;;
    esac

    # Bot identity
    echo
    print_info "Bot identity (used in CLAUDE.md and first-startup message):"
    read -p "$(echo -e "${YELLOW}?${NC} Bot name (default: Clawdy): ")" bot_name_input
    BOT_NAME="${bot_name_input:-Clawdy}"

    read -p "$(echo -e "${YELLOW}?${NC} Bot purpose, one line (default: your personal AI assistant): ")" bot_purpose_input
    BOT_PURPOSE="${bot_purpose_input:-your personal AI assistant}"

    print_success "Configuration complete"
}

# Step 6: Write .env file (merge defaults from default.env, then apply real values)
write_env_file() {
    local env_file="$SCRIPT_DIR/.env"
    local default_env="$SCRIPT_DIR/default.env"

    print_info "Writing configuration..."

    # Start from default.env template if .env doesn't exist yet
    if [ ! -f "$env_file" ] && [ -f "$default_env" ]; then
        cp "$default_env" "$env_file"
        print_info "Initialised .env from default.env"
    elif [ ! -f "$env_file" ]; then
        touch "$env_file"
    fi

    # Helper: set/replace a key=value line in the .env file
    set_env_var() {
        local key="$1"
        local value="$2"
        if grep -q "^${key}=" "$env_file"; then
            # Replace existing line (handles blank values too)
            sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
        else
            echo "${key}=${value}" >> "$env_file"
        fi
    }

    set_env_var "SETUP_USER"           "$SETUP_USER"
    set_env_var "EASYCLAW_DIR"         "$SCRIPT_DIR"
    set_env_var "SESSION_ID"           "$SESSION_ID"
    set_env_var "CLAUDE_DEFAULT_MODEL" "$CLAUDE_MODEL"
    set_env_var "BOT_NAME"             "${BOT_NAME:-Clawdy}"
    set_env_var "BOT_PURPOSE"          "${BOT_PURPOSE:-your personal AI assistant}"

    if [ -n "${TELEGRAM_TOKEN:-}" ]; then
        set_env_var "TELEGRAM_BOT_TOKEN" "$TELEGRAM_TOKEN"
    fi

    chmod 600 "$env_file"
    print_success "Configuration saved to $env_file"
}

# Step 6b: Install clawdy-mcp Python server and register in Claude settings
install_mcp_server() {
    print_info "Installing clawdy-mcp server..."
    log "DEBUG" "MCP server: $SCRIPT_DIR/clawdy-mcp.py"

    if [ ! -f "$SCRIPT_DIR/clawdy-mcp.py" ]; then
        print_warn "clawdy-mcp.py not found — skipping MCP setup"
        return 0
    fi

    # Install required Python packages
    # --break-system-packages needed on Ubuntu 22.04+ (PEP 668 externally managed env)
    print_info "Installing Python dependencies (mcp, requests)..."
    if pip3 install --quiet --break-system-packages mcp requests; then
        log "INFO" "Python packages installed: mcp, requests"
        print_success "Python packages installed"
    else
        print_warn "pip3 install failed — MCP server may not work"
        log "WARN" "pip3 install mcp requests failed"
    fi

    # Register via claude mcp add (writes to ~/.claude.json project config)
    # Note: mcpServers in settings.json is for Claude Desktop, NOT Claude Code CLI
    if claude mcp add clawdy-mcp -- python3 "$SCRIPT_DIR/clawdy-mcp.py" 2>/dev/null; then
        print_success "Registered clawdy-mcp via claude mcp add"
        log "INFO" "MCP server registered: python3 $SCRIPT_DIR/clawdy-mcp.py"
    else
        print_warn "claude mcp add failed — try manually: claude mcp add clawdy-mcp -- python3 $SCRIPT_DIR/clawdy-mcp.py"
        log "WARN" "claude mcp add failed"
    fi

    # Set the default model in settings.json
    local settings="$HOME/.claude/settings.json"
    mkdir -p "$HOME/.claude"
    if [ -f "$settings" ] && command -v jq &>/dev/null; then
        jq --arg model "$CLAUDE_MODEL" '.model = $model' \
           "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
        log "INFO" "Set model to $CLAUDE_MODEL in settings.json"
    fi
}

# Step 7: Install claude-start.sh wrapper script
install_start_script() {
    print_info "Installing claude-start.sh..."

    local template="$SCRIPT_DIR/claude-start.sh.template"
    local dest="$HOME/claude-start.sh"

    if [ ! -f "$template" ]; then
        print_error "claude-start.sh.template not found in $SCRIPT_DIR"
        return 1
    fi

    sed \
        -e "s|%%HOME%%|$HOME|g" \
        -e "s|%%SESSION_ID%%|$SESSION_ID|g" \
        -e "s|%%BOT_NAME%%|$BOT_NAME|g" \
        "$template" > "$dest"

    chmod +x "$dest"
    print_success "Installed claude-start.sh"
    log "INFO" "claude-start.sh written to $dest"
}

# Step 8: Install systemd services
install_services() {
    print_info "Installing systemd services..."

    if [ ! -d "$SCRIPT_DIR/services" ]; then
        print_error "services/ directory not found in $SCRIPT_DIR"
        return 1
    fi

    for service_file in "$SCRIPT_DIR"/services/*.service; do
        if [ ! -f "$service_file" ]; then
            continue
        fi

        local service_name=$(basename "$service_file")
        local dest="/etc/systemd/system/$service_name"
        local temp_service=$(mktemp)

        # Substitute placeholders
        sed \
            -e "s|%%USER%%|$SETUP_USER|g" \
            -e "s|%%HOME%%|$(eval echo ~$SETUP_USER)|g" \
            -e "s|%%EASYCLAW_DIR%%|$SCRIPT_DIR|g" \
            -e "s|%%SESSION_ID%%|$SESSION_ID|g" \
            -e "s|%%CLAUDE_MODEL%%|$CLAUDE_MODEL|g" \
            "$service_file" > "$temp_service"

        # Install with sudo
        sudo tee "$dest" > /dev/null < "$temp_service"
        sudo chmod 644 "$dest"
        rm "$temp_service"

        print_success "Installed $service_name"
    done

    print_info "Reloading systemd daemon..."
    sudo systemctl daemon-reload

    for service_file in "$SCRIPT_DIR"/services/*.service; do
        if [ ! -f "$service_file" ]; then
            continue
        fi
        local service_name=$(basename "$service_file")
        sudo systemctl enable "$service_name"
    done

    print_success "Services enabled"
}

# Step 8: Security hardening
harden_security() {
    print_info "Security hardening..."

    # UFW firewall
    if confirm "Enable UFW firewall (open SSH 22, Tailscale 41641)?"; then
        if ! command -v ufw &> /dev/null; then
            print_warn "UFW not installed, installing..."
            sudo apt install -y ufw
        fi

        sudo ufw allow 22/tcp
        sudo ufw allow 41641/udp
        sudo ufw --force enable
        print_success "UFW configured"
    fi

    # Tailscale
    if confirm "Setup Tailscale (optional VPN for secure remote access)?"; then
        if ! command -v tailscale &> /dev/null; then
            print_info "Installing Tailscale..."
            curl -fsSL https://tailscale.com/install.sh | sh
        fi

        print_info "Starting Tailscale..."
        sudo tailscale up
        print_success "Tailscale configured"
    fi
}

# Step 9: Start services
start_services() {
    print_info "Starting services..."

    for service_file in "$SCRIPT_DIR"/services/*.service; do
        if [ ! -f "$service_file" ]; then
            continue
        fi

        local service_name=$(basename "$service_file")

        if sudo systemctl start "$service_name"; then
            print_success "Started $service_name"
        else
            print_warn "Failed to start $service_name - check logs: sudo journalctl -u $service_name -n 50"
        fi
    done
}

# Main execution
main() {
    log "INFO" "Starting EasyClaw setup (version: $(date +%Y-%m-%d))"
    log "INFO" "Running as user: $USER, Script dir: $SCRIPT_DIR"
    log "INFO" "Verbose mode: $VERBOSE"

    echo
    echo "╔════════════════════════════════════════╗"
    echo "║     EasyClaw Setup — Claude Code       ║"
    echo "║   + Telegram Bot on VPS                ║"
    echo "╚════════════════════════════════════════╝"
    echo
    [ "$VERBOSE" = "1" ] && echo "📝 Logging to: $LOG_FILE" && echo

    handle_resume

    is_done "dependencies" || { check_dependencies || { log "ERROR" "Dependency check failed"; return 1; }; mark_done "dependencies"; }
    is_done "session_id"   || { init_session_id; mark_done "session_id"; }
    is_done "oauth"        || { first_launch_claude || { log "ERROR" "First launch setup failed"; return 1; }; mark_done "oauth"; }
    is_done "claude_json"  || { patch_claude_json; mark_done "claude_json"; }
    is_done "config"       || { collect_config || { log "ERROR" "Configuration collection failed"; return 1; }; mark_done "config"; }
    is_done "identity"     || { install_bot_identity; mark_done "identity"; }
    is_done "env_file"     || { write_env_file; mark_done "env_file"; }
    is_done "mcp_server"   || { install_mcp_server; mark_done "mcp_server"; }
    is_done "start_script" || { install_start_script; mark_done "start_script"; }

    echo
    print_info "Next: Install systemd services (requires sudo)"
    if ! confirm "Continue with systemd installation?"; then
        print_warn "Setup paused. Run this script again to continue."
        log "INFO" "Setup paused by user"
        save_state
        return 0
    fi

    is_done "services" || { install_services || { log "ERROR" "Service installation failed"; return 1; }; mark_done "services"; }

    echo
    if confirm "Enable security hardening (UFW + Tailscale)?"; then
        harden_security
    fi

    echo
    print_info "Starting services..."
    start_services
    rm -f "$STATE_FILE"
    log "INFO" "Setup complete — state file removed"

    echo
    print_success "EasyClaw setup complete!"
    echo
    echo "  Next steps:"
    echo "    • Check service status: sudo systemctl status claude-code"
    echo "    • View logs: sudo journalctl -u claude-code -f"
    echo "    • Configure Telegram: Send /start to your bot"
    echo
    echo "  📝 Setup log: $LOG_FILE"
    [ "$VERBOSE" = "1" ] && echo "  Debug output above ↑"
    echo

    log "INFO" "Setup completed successfully"
}

main "$@"
