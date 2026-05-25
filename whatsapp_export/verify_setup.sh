#!/bin/bash

set -euo pipefail

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${HOME}/.whatsapp_export.conf"           # legacy rsync flow
INGEST_CONF="${MIKOSHI_INGEST_CONF:-${HOME}/.mikoshi-ingest.conf}"  # HTTP API flow

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

PACKAGES=("iphone_backup_decrypt" "cryptography" "rich" "questionary" "jsonschema")
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
        echo "  2. For the first run, connect iPhone via USB cable (easier)"
        echo "  3. To enable WiFi sync later: connect via USB, open Finder on Mac,"
        echo "     select iPhone in sidebar, tab 'General', tick 'Show this iPhone when on Wi-Fi'"
        echo "  3. iPhone is unlocked"
        echo ""
    fi
else
    warn "idevice_id not available (will be ready after setup.sh)"
fi

# Remote sync configuration — Mikoshi HTTP ingest (current) or rsync (legacy)
echo ""
echo "Remote Sync Configuration:"
echo ""

# Preferred: HTTP push to Mikoshi
if [[ -f "$INGEST_CONF" ]] || [[ -n "${MIKOSHI_URL:-}" && -n "${MIKOSHI_TOKEN:-}" ]]; then
    if [[ -f "$INGEST_CONF" ]]; then
        # Use env -i + parse so we don't pollute the current shell
        MIKOSHI_URL=$(grep -E '^MIKOSHI_URL=' "$INGEST_CONF" | head -1 | cut -d= -f2- | tr -d '"' )
        MIKOSHI_TOKEN=$(grep -E '^MIKOSHI_TOKEN=' "$INGEST_CONF" | head -1 | cut -d= -f2- | tr -d '"' )
        pass "Mikoshi ingest config found: $INGEST_CONF"
        # Permission check — token should not be world-readable
        perm=$(stat -f '%Lp' "$INGEST_CONF" 2>/dev/null || stat -c '%a' "$INGEST_CONF" 2>/dev/null)
        if [[ "$perm" != "600" ]]; then
            warn "  Permissions are $perm; recommend 'chmod 600 $INGEST_CONF' (contains a token)"
        fi
    else
        pass "Mikoshi ingest config via environment variables"
    fi

    if [[ -n "${MIKOSHI_URL:-}" ]]; then
        info "MIKOSHI_URL: $MIKOSHI_URL"
        if curl -sS -o /dev/null -m 5 -w "%{http_code}" "$MIKOSHI_URL" 2>/dev/null | grep -qE '^[23]'; then
            pass "Mikoshi server reachable"
        else
            warn "Could not reach $MIKOSHI_URL (server down, DNS, or VPN?)"
        fi
    else
        warn "MIKOSHI_URL not set"
    fi

    if [[ -n "${MIKOSHI_TOKEN:-}" ]]; then
        pass "MIKOSHI_TOKEN is set (${#MIKOSHI_TOKEN} chars)"
    else
        warn "MIKOSHI_TOKEN not set"
    fi
elif [[ -f "$CONFIG_FILE" ]]; then
    # Legacy rsync flow — still supported but discouraged
    source "$CONFIG_FILE"
    warn "Legacy rsync config detected: $CONFIG_FILE"
    info "  Project now uses HTTP push (push_via_api.py). Migrate when convenient:"
    info "  see whatsapp_export/push_via_api.py for MIKOSHI_URL/MIKOSHI_TOKEN."
else
    warn "No Mikoshi config found"
    echo ""
    echo "  To push exports to your Mikoshi server, create ~/.mikoshi-ingest.conf:"
    echo ""
    echo "      MIKOSHI_URL=https://mikoshi.your-domain.com"
    echo "      MIKOSHI_TOKEN=<generate-from-/accounts/<id>/ingestion-on-mikoshi>"
    echo ""
    echo "  Then: chmod 600 ~/.mikoshi-ingest.conf"
    echo ""
    echo "  This is optional — you can run --skip-remote-sync to test locally first."
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
