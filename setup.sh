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
USER_HOME=""
CLAUDE_MODEL=""
TELEGRAM_TOKEN=""
SESSION_ID=""
BOT_NAME=""
BOT_PURPOSE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERBOSE="${VERBOSE:-0}"
LOG_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/setup.log"
STATE_FILE="$HOME/.easyclaw-setup-state"
COMPLETED_STEPS=()

# Ensure ~/.local/bin is always on PATH (claude installed there by official installer)
export PATH="$HOME/.local/bin:$PATH"

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
    local status="${1:-}"
    cat > "$STATE_FILE" << EOF
SETUP_USER="$SETUP_USER"
CLAUDE_MODEL="$CLAUDE_MODEL"
TELEGRAM_TOKEN="$TELEGRAM_TOKEN"
SESSION_ID="$SESSION_ID"
BOT_NAME="$BOT_NAME"
BOT_PURPOSE="$BOT_PURPOSE"
COMPLETED_STEPS=(${COMPLETED_STEPS[*]+"${COMPLETED_STEPS[*]}"})
SETUP_STATUS="${status}"
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

    # Check if setup was previously completed
    local setup_status
    setup_status=$(grep "^SETUP_STATUS=" "$STATE_FILE" 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "")

    if [ "$setup_status" = "COMPLETE" ]; then
        echo
        echo -e "${GREEN}✓ EasyClaw is already installed.${NC}"
        echo
        if confirm "Reconfigure / reinstall? (n = exit)"; then
            rm -f "$STATE_FILE"
            print_info "Starting fresh..."
            log "INFO" "User chose to reinstall"
        else
            print_info "Nothing to do. Exiting."
            exit 0
        fi
        return 0
    fi

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
        "pip3:python3-pip"
        "tmux:tmux"
        "ffmpeg:ffmpeg"
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
# Step 3: First-launch Claude setup — OAuth only, no extra flags
first_launch_claude() {
    # Check if already logged in (either auth file location)
    if [ -f "${USER_HOME}/.claude/claude.json" ] || [ -f "${USER_HOME}/.claude.json" ]; then
        print_success "Claude Code already authenticated"
        return 0
    fi

    print_info "Starting Claude Code initial setup (OAuth login)..."
    echo
    echo -e "  ${YELLOW}A browser window will open for Claude OAuth login.${NC}"
    echo    "  Log in, then type '/exit' and press Enter to continue setup."
    echo

    local claude_bin="${USER_HOME}/.local/bin/claude"
    # Run from USER_HOME so the session is created in the right project directory
    local launch_cmd="cd $USER_HOME && IS_SANDBOX=1 $claude_bin"

    if [ "$(whoami)" != "$SETUP_USER" ]; then
        su - "$SETUP_USER" -c "$launch_cmd"
    else
        eval "$launch_cmd"
    fi

    print_success "Claude Code session complete"
    log "INFO" "First launch done"
}

# Read session ID from the newest JSONL Claude created (run after first_launch_claude)
init_session_id() {
    print_info "Reading session ID..."
    local session_file="${USER_HOME}/.claude/session-id"

    # Find newest JSONL across all project dirs — that's the session just created
    local projects_dir="${USER_HOME}/.claude/projects"
    local latest_jsonl
    latest_jsonl=$(find "$projects_dir" -name "*.jsonl" -printf "%T@ %p\n" 2>/dev/null \
        | sort -n | tail -1 | awk '{print $2}')

    if [ -n "$latest_jsonl" ]; then
        local new_id
        new_id=$(basename "$latest_jsonl" .jsonl)
        # Only update if different from saved (i.e. a new session was created)
        if [ "$new_id" != "$SESSION_ID" ]; then
            SESSION_ID="$new_id"
            echo "$SESSION_ID" > "$session_file"
            chmod 600 "$session_file"
            print_success "Session ID: $SESSION_ID"
            log "INFO" "Session ID saved from: $latest_jsonl"
        else
            print_success "Session ID unchanged: $SESSION_ID"
        fi
    elif [ -f "$session_file" ]; then
        SESSION_ID=$(cat "$session_file")
        print_success "Session ID (from file): $SESSION_ID"
    else
        print_error "No session found — did the first launch complete?"
        return 1
    fi
}

# Step 4: Trust home directory in ~/.claude.json
# Trust dialog state lives in ~/.claude.json under projects["<path>"]["hasTrustDialogAccepted"]
patch_claude_json() {
    print_info "Configuring Claude Code settings..."
    local claude_json="${USER_HOME}/.claude.json"

    if [ ! -f "$claude_json" ]; then
        print_warn "~/.claude.json not found — creating minimal config"
        echo '{}' > "$claude_json"
    fi

    # Check if already trusted
    if jq -e ".projects[\"$USER_HOME\"].hasTrustDialogAccepted == true" "$claude_json" &>/dev/null; then
        print_success "Home directory already trusted in Claude Code"
        return 0
    fi

    # Set hasTrustDialogAccepted for the home directory project
    jq ".projects[\"$USER_HOME\"].hasTrustDialogAccepted = true" "$claude_json" > "$claude_json.tmp"
    mv "$claude_json.tmp" "$claude_json"
    print_success "Home directory trusted in Claude Code"
}

# Step 5: Install CLAUDE.md — full operational instructions for the agent
install_bot_identity() {
    local src="$SCRIPT_DIR/bot-identity.md"
    local dest_claude_md="${USER_HOME}/CLAUDE.md"

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

# Step 5b: Collect configuration from user (saves after each prompt so Ctrl+C is safe)
collect_config() {
    print_info "Configuring EasyClaw..."

    # Username
    if [ -z "$SETUP_USER" ]; then
        local default_user="${USER:-root}"

        read -p "$(echo -e "${YELLOW}?${NC} System username (default: $default_user): ")" username
        username="${username:-$default_user}"
        if ! id "$username" &> /dev/null; then
            print_error "User $username does not exist"
            return 1
        fi
        SETUP_USER="$username"
        save_state
    else
        print_success "System username: $SETUP_USER (saved)"
    fi

    # Set USER_HOME to the target user's home (may differ from $HOME when run as root)
    USER_HOME=$(eval echo "~$SETUP_USER")
    log "INFO" "USER_HOME set to: $USER_HOME"

    # Telegram token
    if [ -z "$TELEGRAM_TOKEN" ]; then
        read -p "$(echo -e "${YELLOW}?${NC} Telegram bot token (optional, press enter to skip): ")" token
        if [ -n "$token" ]; then
            if [[ ! $token =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
                print_warn "Token format looks incorrect (should be: 123456:ABC-DEF...)"
                if confirm "Use anyway?"; then
                    TELEGRAM_TOKEN="$token"
                fi
            else
                # Validate against Telegram API before accepting
                print_info "Validating token with Telegram API..."
                local api_resp
                api_resp=$(curl -sf --max-time 10 "https://api.telegram.org/bot${token}/getMe" 2>/dev/null || echo "")
                if [ -n "$api_resp" ] && echo "$api_resp" | jq -e '.ok == true' &>/dev/null; then
                    local bot_username
                    bot_username=$(echo "$api_resp" | jq -r '.result.username // "unknown"')
                    print_success "Token valid — bot: @${bot_username}"
                    log "INFO" "Telegram token validated: @${bot_username}"
                    TELEGRAM_TOKEN="$token"
                else
                    print_warn "Telegram API rejected the token (check it's correct and the bot exists)"
                    log "WARN" "Telegram getMe failed for provided token"
                    if confirm "Use anyway?"; then
                        TELEGRAM_TOKEN="$token"
                    fi
                fi
            fi
        else
            TELEGRAM_TOKEN="skip"  # sentinel so we don't re-ask on resume
        fi
        save_state
    else
        [ "$TELEGRAM_TOKEN" = "skip" ] && print_success "Telegram token: skipped (saved)" \
            || print_success "Telegram token: configured (saved)"
    fi
    [ "$TELEGRAM_TOKEN" = "skip" ] && TELEGRAM_TOKEN=""

    # Claude model
    if [ -z "$CLAUDE_MODEL" ]; then
        echo
        print_info "Default Claude model:"
        echo "  1) Haiku (fast, cheap) - RECOMMENDED"
        echo "  2) Sonnet (balanced)"
        echo "  3) Opus (most capable)"
        read -p "$(echo -e "${YELLOW}?${NC} Choose (1-3, default 1): ")" model_choice
        case "${model_choice:-1}" in
            1) CLAUDE_MODEL="haiku" ;;
            2) CLAUDE_MODEL="sonnet" ;;
            3) CLAUDE_MODEL="opus" ;;
            *) print_warn "Invalid choice, using haiku"; CLAUDE_MODEL="haiku" ;;
        esac
        save_state
    else
        print_success "Model: $CLAUDE_MODEL (saved)"
    fi

    # Bot identity
    if [ -z "$BOT_NAME" ]; then
        echo
        print_info "Bot identity:"
        read -p "$(echo -e "${YELLOW}?${NC} Bot name (default: Clawdy): ")" bot_name_input
        BOT_NAME="${bot_name_input:-Clawdy}"
        save_state
    else
        print_success "Bot name: $BOT_NAME (saved)"
    fi

    if [ -z "$BOT_PURPOSE" ]; then
        read -p "$(echo -e "${YELLOW}?${NC} Bot purpose (default: your personal AI assistant): ")" bot_purpose_input
        BOT_PURPOSE="${bot_purpose_input:-your personal AI assistant}"
        save_state
    else
        print_success "Bot purpose: $BOT_PURPOSE (saved)"
    fi

    print_success "Configuration complete"
}

# Step 6: Write .env file (merge defaults from default.env, then apply real values)
write_env_file() {
    local env_file="${USER_HOME}/.easyclaw/.env"
    local default_env="$SCRIPT_DIR/default.env"

    print_info "Writing configuration..."

    # Ensure workspace dir exists
    mkdir -p "${USER_HOME}/.easyclaw"

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
    log "DEBUG" "MCP server: $SCRIPT_DIR/workspace/scripts/clawdy-mcp.py"

    if [ ! -f "$SCRIPT_DIR/workspace/scripts/clawdy-mcp.py" ]; then
        print_warn "workspace/scripts/clawdy-mcp.py not found — skipping MCP setup"
        return 0
    fi

    # Install Python packages system-wide
    # --break-system-packages: required on Ubuntu 22.04+ (PEP 668)
    # --ignore-installed: skip uninstall step, avoiding RECORD file errors on apt-managed packages
    print_info "Installing Python dependencies (mcp, requests, faster-whisper)..."
    if python3 -m pip install --quiet --break-system-packages --ignore-installed mcp requests faster-whisper 2>&1 | tee -a "$LOG_FILE"; then
        log "INFO" "Python packages installed: mcp, requests, faster-whisper"
        print_success "Python packages installed"
    else
        print_warn "pip install failed — MCP server may not work"
        log "WARN" "pip install mcp requests failed"
    fi

    # Copy clawdy-mcp.py to a stable location independent of the repo path
    local mcp_dest="${USER_HOME}/.easyclaw/scripts/clawdy-mcp.py"
    mkdir -p "${USER_HOME}/.easyclaw/scripts"
    cp "$SCRIPT_DIR/workspace/scripts/clawdy-mcp.py" "$mcp_dest"
    chmod +x "$mcp_dest"
    print_success "Installed clawdy-mcp.py -> $mcp_dest"
    log "INFO" "MCP server copied to: $mcp_dest"

    # Register MCP server by patching ~/.claude.json directly
    # (claude mcp add writes to the cwd project, which may not be USER_HOME)
    local claude_json="${USER_HOME}/.claude.json"
    [ -f "$claude_json" ] || echo '{}' > "$claude_json"
    jq ".projects[\"$USER_HOME\"].mcpServers[\"clawdy-mcp\"] = {
          \"type\": \"stdio\",
          \"command\": \"python3\",
          \"args\": [\"$mcp_dest\"],
          \"env\": {}
        }" "$claude_json" > "$claude_json.tmp" && mv "$claude_json.tmp" "$claude_json"
    print_success "Registered clawdy-mcp in ~/.claude.json for $USER_HOME"
    log "INFO" "MCP server registered in ~/.claude.json: python3 $mcp_dest"

    # Set the default model in settings.json
    local settings="${USER_HOME}/.claude/settings.json"
    mkdir -p "${USER_HOME}/.claude"
    # Create settings.json if missing
    [ -f "$settings" ] || echo '{}' > "$settings"
    jq --arg model "$CLAUDE_MODEL" '
      .model = $model |
      .dangerouslySkipPermissions = true |
      .skipDangerousModePermissionPrompt = true
    ' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
    log "INFO" "Set model=$CLAUDE_MODEL and dangerouslySkipPermissions in settings.json"
    print_success "Claude Code settings configured"
}

# Step 7: Install claude-start.sh wrapper script
install_start_script() {
    print_info "Installing claude-start.sh..."

    local template="$SCRIPT_DIR/claude-start.sh.template"
    local dest="${USER_HOME}/claude-start.sh"

    if [ ! -f "$template" ]; then
        print_error "claude-start.sh.template not found in $SCRIPT_DIR"
        return 1
    fi

    sed \
        -e "s|%%HOME%%|$USER_HOME|g" \
        -e "s|%%BOT_NAME%%|$BOT_NAME|g" \
        "$template" > "$dest"

    chmod +x "$dest"
    print_success "Installed claude-start.sh"
    log "INFO" "claude-start.sh written to $dest"
}

# Step 7b: Create workspace directory
create_workspace() {
    print_info "Creating workspace directory ~/.easyclaw/..."
    mkdir -p "${USER_HOME}/.easyclaw/scripts"
    mkdir -p "${USER_HOME}/telegram-files"
    print_success "Workspace directory created: ${USER_HOME}/.easyclaw/"
    log "INFO" "Created ${USER_HOME}/.easyclaw/, scripts/, and telegram-files/"
}

# Step 7c: Install scripts to ~/.easyclaw/scripts/
install_scripts() {
    print_info "Installing scripts to ~/.easyclaw/scripts/..."
    mkdir -p "${USER_HOME}/.easyclaw/scripts"

    local ws="$SCRIPT_DIR/workspace/scripts"

    # Copy all workspace scripts to ~/.easyclaw/scripts/
    for script in clawdy-cron-check.sh clawdy-daily-briefing.sh telegram-bot.py; do
        if [ -f "$ws/$script" ]; then
            cp "$ws/$script" "${USER_HOME}/.easyclaw/scripts/"
            chmod +x "${USER_HOME}/.easyclaw/scripts/$script" 2>/dev/null || true
            print_success "Installed $script"
        else
            print_warn "$script not found in workspace/scripts/ — skipping"
        fi
    done

    # Install clawdy-restart and clawdy-update to /usr/local/bin so they're on PATH
    for bin_script in clawdy-restart update.sh; do
        dest_name="${bin_script/update.sh/clawdy-update}"
        src="$SCRIPT_DIR/$bin_script"
        if [ -f "$src" ]; then
            sudo cp "$src" "/usr/local/bin/$dest_name"
            sudo chmod +x "/usr/local/bin/$dest_name"
            print_success "Installed $dest_name -> /usr/local/bin/"
        else
            print_warn "$bin_script not found in $SCRIPT_DIR — skipping $dest_name"
        fi
    done

    # Make all scripts executable
    chmod +x "${USER_HOME}/.easyclaw/scripts/"*.sh 2>/dev/null || true
    chmod +x "${USER_HOME}/.easyclaw/scripts/"*.py 2>/dev/null || true

    # Set up crontab — use temp file to avoid pipefail killing us when no crontab exists
    local tmp_cron; tmp_cron=$(mktemp)
    crontab -l 2>/dev/null | grep -v "clawdy-cron-check" > "$tmp_cron" || true
    echo "*/30 * * * * ${USER_HOME}/.easyclaw/scripts/clawdy-cron-check.sh" >> "$tmp_cron"
    crontab "$tmp_cron"
    rm -f "$tmp_cron"
    print_success "Crontab updated: cron check every 30 min"
    log "INFO" "Crontab: cron check every 30 min"

    # Note: daily briefing cron is NOT added here — it's opt-in, set up by the agent
    # on first startup if the user wants it.

    print_success "Scripts installed"
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

    # Restore USER_HOME after resume (SETUP_USER loaded from state)
    [ -n "$SETUP_USER" ] && USER_HOME=$(eval echo "~$SETUP_USER") || USER_HOME="$HOME"

    is_done "dependencies" || { check_dependencies || { log "ERROR" "Dependency check failed"; return 1; }; mark_done "dependencies"; }
    is_done "config"       || { collect_config || { log "ERROR" "Configuration collection failed"; return 1; }; mark_done "config"; }

    # Stop any running claude session before OAuth — otherwise the login browser
    # opens inside tmux instead of in this terminal
    if ! is_done "oauth"; then
        systemctl stop claude-code.service 2>/dev/null || true
        tmux kill-session -t claude 2>/dev/null || true
        sleep 1
    fi

    is_done "workspace"    || { create_workspace; mark_done "workspace"; }
    is_done "oauth"        || { first_launch_claude; mark_done "oauth"; }
    is_done "claude_json"  || { patch_claude_json; mark_done "claude_json"; }
    is_done "identity"     || { install_bot_identity; mark_done "identity"; }
    is_done "env_file"     || { write_env_file; mark_done "env_file"; }
    is_done "mcp_server"   || { install_mcp_server; mark_done "mcp_server"; }
    is_done "start_script" || { install_start_script; mark_done "start_script"; }
    is_done "scripts"      || { install_scripts; mark_done "scripts"; }

    # Stop any running services before reinstalling
    for service_file in "$SCRIPT_DIR"/services/*.service; do
        [ -f "$service_file" ] || continue
        local svc=$(basename "$service_file")
        if sudo systemctl is-active --quiet "$svc" 2>/dev/null; then
            print_info "Stopping $svc..."
            sudo systemctl stop "$svc" 2>/dev/null || true
        fi
    done

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
    save_state "COMPLETE"
    log "INFO" "Setup complete — state marked as COMPLETE"

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
