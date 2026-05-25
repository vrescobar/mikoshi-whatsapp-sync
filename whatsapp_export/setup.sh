#!/bin/bash

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
SETUP_LOG="${LOG_DIR}/setup_${TIMESTAMP}.log"

mkdir -p "$LOG_DIR" "${SCRIPT_DIR}/exports" "${SCRIPT_DIR}/temp"

log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "$SETUP_LOG"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "$SETUP_LOG" >&2
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$SETUP_LOG"
}

check_command() {
    if command -v "$1" &> /dev/null; then
        log "✓ $1 is installed"
        return 0
    else
        return 1
    fi
}

log "=== WhatsApp Export Pipeline Setup ==="
log "Checking system requirements..."

# Check macOS
if [[ ! "$OSTYPE" == "darwin"* ]]; then
    error "This script only works on macOS"
    exit 1
fi
log "✓ Running on macOS"

# Check Homebrew
if ! check_command brew; then
    error "Homebrew is not installed. Install from: https://brew.sh"
    exit 1
fi

# Install libimobiledevice and dependencies
log ""
log "Installing libimobiledevice and device communication tools..."
brew install libimobiledevice usbmuxd libusbmuxd openssl@3 libusb 2>&1 | tee -a "$SETUP_LOG"

# Install Python tools
log ""
log "Installing Python dependencies..."

if ! check_command python3; then
    error "Python 3 is not installed"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | awk '{print $2}')
log "Python version: $PYTHON_VERSION"

# Create virtual environment
if [[ ! -d "${SCRIPT_DIR}/.venv" ]]; then
    log "Creating Python virtual environment..."
    python3 -m venv "${SCRIPT_DIR}/.venv"
else
    log "Virtual environment already exists, skipping creation"
fi

# Activate venv
source "${SCRIPT_DIR}/.venv/bin/activate"

# Upgrade pip
log "Upgrading pip..."
python3 -m pip install --upgrade pip 2>&1 | tee -a "$SETUP_LOG"

# Install Python packages
log "Installing Python packages..."
python3 -m pip install \
    iphone-backup-decrypt \
    whatsapp-chat-exporter \
    cryptography \
    pycryptodomex \
    jsonschema \
    pytest \
    2>&1 | tee -a "$SETUP_LOG"

# Verify installations
log ""
log "=== Verifying Installations ==="

for cmd in idevicebackup2 idevice_id ideviceinfo; do
    if check_command "$cmd"; then
        VERSION=$("$cmd" --version 2>&1 || echo "N/A")
        log "  $cmd: $VERSION"
    else
        warn "  $cmd: NOT FOUND (may be installed but not in PATH)"
    fi
done

# Check Python packages
log ""
log "Installed Python packages:"
python3 -m pip list | grep -E "iphone-backup-decrypt|whatsapp-chat-exporter|cryptography" | tee -a "$SETUP_LOG" || warn "Some packages may not be listed"

# Setup macOS Keychain
log ""
log "=== Keychain Setup ==="
log "To store your iPhone backup password securely:"
log ""
log "  security add-generic-password -a 'iphone_backup' -s 'iphone_backup_password' -w 'YOUR_BACKUP_PASSWORD' 2>/dev/null"
log ""
log "Replace YOUR_BACKUP_PASSWORD with your actual iPhone backup password."
log "This will be stored in macOS Keychain and used by the pipeline."
log ""

# Final checklist
log ""
log "=== Setup Complete ==="
log "Next steps:"
log "  1. Enable WiFi Sync on your iPhone: Settings → General → AirDrop & Handoff → WiFi Sync"
log "  2. Store backup password in Keychain (see instructions above)"
log "  3. Configure SSH for remote server sync (optional)"
log "  4. Run: ${SCRIPT_DIR}/run_pipeline.sh"
log ""
log "Setup log saved to: $SETUP_LOG"
