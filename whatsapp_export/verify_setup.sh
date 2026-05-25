#!/bin/bash

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${HOME}/.whatsapp_export.conf"

pass() {
    echo -e "${GREEN}✓${NC} $1"
}

fail() {
    echo -e "${RED}✗${NC} $1"
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}WhatsApp Export Pipeline - Setup Verification${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""

# System checks
echo "System Requirements:"
echo ""

# macOS check
if [[ "$OSTYPE" == "darwin"* ]]; then
    pass "macOS detected"
    OS_VERSION=$(sw_vers -productVersion)
    info "Version: $OS_VERSION"
else
    fail "Not running on macOS"
    exit 1
fi

# Homebrew
if command -v brew &> /dev/null; then
    pass "Homebrew installed"
else
    fail "Homebrew not installed. Install from: https://brew.sh"
    exit 1
fi

# Python
echo ""
echo "Python Environment:"
echo ""

if command -v python3 &> /dev/null; then
    PY_VERSION=$(python3 --version 2>&1)
    pass "$PY_VERSION"
else
    fail "Python 3 not found"
    exit 1
fi

# Virtual environment
echo ""
echo "Virtual Environment:"
echo ""

if [[ -d "${SCRIPT_DIR}/.venv" ]]; then
    pass "Python venv exists at ${SCRIPT_DIR}/.venv"
    source "${SCRIPT_DIR}/.venv/bin/activate"
    pass "Virtual environment activated"
else
    fail "Python venv not found. Run: bash setup.sh"
    exit 1
fi

# Installed tools
echo ""
echo "Mobile Device Tools:"
echo ""

TOOLS=("idevice_id" "idevicebackup2" "ideviceinfo")
for tool in "${TOOLS[@]}"; do
    if command -v "$tool" &> /dev/null; then
        VERSION=$("$tool" --version 2>&1 | head -1 || echo "version unknown")
        pass "$tool"
        info "  $VERSION"
    else
        fail "$tool not found"
    fi
done

# Python packages
echo ""
echo "Python Packages:"
echo ""

PACKAGES=("iphone_backup_decrypt" "whatsapp_chat_exporter" "cryptography")
for package in "${PACKAGES[@]}"; do
    if python3 -c "import ${package//-/_}" 2>/dev/null; then
        pass "$(python3 -c "import ${package//-/_}; print('${package//-/_}')")"
    else
        warn "$package not installed (will be installed if needed)"
    fi
done

# Directory structure
echo ""
echo "Directory Structure:"
echo ""

DIRS=("logs" "exports" "temp")
for dir in "${DIRS[@]}"; do
    if [[ -d "${SCRIPT_DIR}/$dir" ]]; then
        pass "${SCRIPT_DIR}/$dir"
    else
        fail "${SCRIPT_DIR}/$dir not found"
    fi
done

# Scripts
echo ""
echo "Pipeline Scripts:"
echo ""

SCRIPTS=("setup.sh" "run_pipeline.sh")
for script in "${SCRIPTS[@]}"; do
    if [[ -f "${SCRIPT_DIR}/$script" ]] && [[ -x "${SCRIPT_DIR}/$script" ]]; then
        pass "$script (executable)"
    else
        fail "$script not found or not executable"
    fi
done

# Keychain password
echo ""
echo "Keychain Configuration:"
echo ""

if security find-generic-password -a iphone_backup -s iphone_backup_password -w &>/dev/null; then
    pass "iPhone backup password stored in Keychain"
else
    warn "iPhone backup password NOT in Keychain"
    echo ""
    echo "  To set it up, run:"
    echo "  security add-generic-password -a iphone_backup -s iphone_backup_password -w 'YOUR_PASSWORD'"
    echo ""
fi

# iPhone detection
echo ""
echo "iPhone Detection:"
echo ""

if command -v idevice_id &> /dev/null; then
    DEVICES=$(idevice_id -l | wc -l)
    if [[ $DEVICES -gt 0 ]]; then
        DEVICE=$(idevice_id -l | head -1)
        pass "iPhone detected: $DEVICE"
        DEVICE_NAME=$(ideviceinfo -u "$DEVICE" 2>/dev/null | grep "DeviceName" | cut -d':' -f2 | xargs || echo "unknown")
        info "Device name: $DEVICE_NAME"
    else
        warn "No iPhone detected"
        echo ""
        echo "  This is OK if you haven't connected yet. Make sure:"
        echo "  1. iPhone and Mac are on same WiFi"
        echo "  2. WiFi Sync is enabled: iPhone Settings > General > AirDrop & Handoff > WiFi Sync"
        echo "  3. iPhone is unlocked"
        echo ""
    fi
else
    warn "idevice_id not available (will be ready after setup.sh)"
fi

# Remote sync configuration
echo ""
echo "Remote Sync Configuration:"
echo ""

if [[ -f "$CONFIG_FILE" ]]; then
    source "$CONFIG_FILE"
    pass "Configuration file found: $CONFIG_FILE"

    if [[ -n "${SSH_HOST:-}" ]]; then
        pass "SSH_HOST configured: $SSH_HOST"
        info "SSH_USER: ${SSH_USER:-not set}"
        info "SSH_PATH: ${SSH_PATH:-not set}"

        # Test SSH connection
        if ssh -o ConnectTimeout=3 "$SSH_USER@$SSH_HOST" echo "SSH connection OK" &>/dev/null; then
            pass "SSH connection successful"
        else
            warn "Cannot connect to SSH server"
            echo ""
            echo "  To test manually:"
            echo "  ssh $SSH_USER@$SSH_HOST echo 'test'"
            echo ""
        fi
    else
        warn "SSH_HOST not configured (remote sync will be skipped)"
    fi
else
    warn "Configuration file not found: $CONFIG_FILE"
    echo ""
    echo "  This is optional. To enable remote sync, create it:"
    echo "  cp .whatsapp_export.conf.example ~/.whatsapp_export.conf"
    echo "  # Then edit with your SSH details"
    echo ""
fi

# Final summary
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo ""

if [[ $DEVICES -gt 0 ]] && security find-generic-password -a iphone_backup -s iphone_backup_password -w &>/dev/null 2>&1; then
    echo -e "${GREEN}✓ Setup looks good! You can run: bash run_pipeline.sh${NC}"
elif [[ $DEVICES -gt 0 ]]; then
    echo -e "${YELLOW}⚠ Setup almost ready!${NC}"
    echo ""
    echo "  Still need to store backup password in Keychain:"
    echo "  security add-generic-password -a iphone_backup -s iphone_backup_password -w 'YOUR_PASSWORD'"
elif security find-generic-password -a iphone_backup -s iphone_backup_password -w &>/dev/null 2>&1; then
    echo -e "${YELLOW}⚠ Setup almost ready!${NC}"
    echo ""
    echo "  Please connect iPhone and try again."
else
    echo -e "${YELLOW}⚠ Setup needs attention:${NC}"
    echo ""
    echo "  1. Store backup password: security add-generic-password -a iphone_backup -s iphone_backup_password -w 'YOUR_PASSWORD'"
    echo "  2. Connect iPhone to the same WiFi"
    echo "  3. Run this script again"
    echo ""
fi

echo ""
