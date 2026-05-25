#!/bin/bash

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
EXPORTS_DIR="${SCRIPT_DIR}/exports"
ATTACHMENTS_DIR="${SCRIPT_DIR}/exports/attachments"
TEMP_DIR="${SCRIPT_DIR}/temp"
STATE_FILE="${SCRIPT_DIR}/.sync_state.json"
LOCK_FILE="${SCRIPT_DIR}/.pipeline.lock"
CONFIG_FILE="${HOME}/.whatsapp_export.conf"
EXTRACTOR="${SCRIPT_DIR}/extract_messages.py"
VALIDATOR="${SCRIPT_DIR}/validate_export.py"
SCHEMA_FILE="${SCRIPT_DIR}/schema.json"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PIPELINE_LOG="${LOG_DIR}/pipeline_${TIMESTAMP}.log"

SYNC_MODE="incremental"
TARGET_CONTACT=""
SKIP_SYNC=false
INCLUDE_SYSTEM=false
KEEP_LOCAL_EXPORTS="${KEEP_LOCAL_EXPORTS:-5}"

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

Options:
  --mode <incremental|full|full-contact>
        Default: incremental.
  --contact <name-or-jid>
        Required when --mode=full-contact.
  --include-system
        Include WhatsApp system messages (group events, encryption notices).
  --skip-remote-sync
        Run extraction but don't rsync to Mikoshi server.
  --keep-local <N>
        Override KEEP_LOCAL_EXPORTS (default 5). Older exports are shredded
        after successful remote sync.
  --help, -h
        Show this message.

Examples:
  $(basename "$0")
  $(basename "$0") --mode full
  $(basename "$0") --mode full-contact --contact "Alice"
  $(basename "$0") --include-system
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) SYNC_MODE="$2"; shift 2 ;;
        --contact) TARGET_CONTACT="$2"; shift 2 ;;
        --include-system) INCLUDE_SYSTEM=true; shift ;;
        --skip-remote-sync) SKIP_SYNC=true; shift ;;
        --keep-local) KEEP_LOCAL_EXPORTS="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ "$SYNC_MODE" == "full-contact" && -z "$TARGET_CONTACT" ]]; then
    echo "ERROR: --mode full-contact requires --contact"
    exit 1
fi

mkdir -p "$LOG_DIR" "$EXPORTS_DIR" "$ATTACHMENTS_DIR" "$TEMP_DIR"

exec > >(tee -a "$PIPELINE_LOG") 2>&1

log()   { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"; }
error() { echo -e "${RED}[ERROR $(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" >&2; }
warn()  { echo -e "${YELLOW}[WARN $(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"; }
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }

if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    log "Configuration loaded from $CONFIG_FILE"
else
    warn "$CONFIG_FILE not found. Remote sync will be skipped."
fi

cleanup() {
    local exit_code=$?
    log "=== Cleanup (exit $exit_code) ==="
    if [[ -d "$TEMP_DIR" ]]; then
        find "$TEMP_DIR" -type f \( \
            -name "ChatStorage.sqlite" -o \
            -name "*.plist" -o \
            -name "Manifest.db" -o \
            -name "Status" \
        \) -exec shred -vfz -n 7 {} \; 2>/dev/null || true
        rm -rf "$TEMP_DIR"
        log "✓ Temp cleaned"
    fi
    rm -f "$LOCK_FILE"
    if [[ $exit_code -eq 0 ]]; then
        log "=== Pipeline OK ==="
    else
        error "=== Pipeline failed (exit $exit_code) ==="
    fi
    exit $exit_code
}
trap cleanup EXIT

acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        error "Pipeline already running (lock: $LOCK_FILE)"
        error "If stale: rm $LOCK_FILE"
        exit 1
    fi
    echo $$ > "$LOCK_FILE"
}

setup_python_env() {
    if [[ -z "${VIRTUAL_ENV:-}" ]]; then
        if [[ -d "${SCRIPT_DIR}/.venv" ]]; then
            # shellcheck disable=SC1091
            source "${SCRIPT_DIR}/.venv/bin/activate"
        else
            error "Python venv missing. Run: bash setup.sh"
            exit 1
        fi
    fi
}

# Map idevicebackup2 stderr patterns to actionable messages
diagnose_backup_error() {
    local err_file="$1"
    if grep -qiE "password|passcode|wrong" "$err_file"; then
        error "Backup password rejected by device."
        error "Either the password in Keychain is wrong, or you changed it on the iPhone."
        error "Fix: security delete-generic-password -a iphone_backup -s iphone_backup_password"
        error "     security add-generic-password -a iphone_backup -s iphone_backup_password -w 'NEW_PASSWORD'"
        return
    fi
    if grep -qiE "locked|passcode protected" "$err_file"; then
        error "iPhone is locked. Unlock the device and re-run."
        return
    fi
    if grep -qiE "trust|pairing|not paired" "$err_file"; then
        error "iPhone has not trusted this Mac yet."
        error "On iPhone: tap 'Trust' when prompted, then re-run."
        return
    fi
    if grep -qiE "no device|not found|ENODEV" "$err_file"; then
        error "Device disappeared mid-backup. Check WiFi or USB cable."
        return
    fi
    if grep -qiE "ENOSPC|no space" "$err_file"; then
        error "Disk full. Free space and re-run."
        return
    fi
    error "Backup failed. Last 20 lines of stderr:"
    tail -20 "$err_file" >&2
}

detect_device() {
    log "=== PHASE 1: Device Detection ==="
    if ! command -v idevice_id &>/dev/null; then
        error "idevice_id not found. Run: bash setup.sh"
        return 1
    fi
    DEVICE_UDID=$(idevice_id -l | head -n1)
    if [[ -z "$DEVICE_UDID" ]]; then
        error "No iPhone detected."
        error "  1. Unlock iPhone"
        error "  2. Same WiFi as Mac (or USB)"
        error "  3. WiFi Sync enabled: Settings → General → AirDrop & Handoff → WiFi Sync"
        return 1
    fi
    log "✓ iPhone: $DEVICE_UDID"
    if ideviceinfo -u "$DEVICE_UDID" >/dev/null 2>&1; then
        DEVICE_NAME=$(ideviceinfo -u "$DEVICE_UDID" | grep "DeviceName" | cut -d':' -f2 | xargs)
        log "✓ Device: $DEVICE_NAME"
    else
        error "Cannot communicate with device. Trust prompt accepted?"
        return 1
    fi
}

create_backup() {
    log "=== PHASE 2: Encrypted Backup ==="
    BACKUP_PATH="${TEMP_DIR}/backup"
    mkdir -p "$BACKUP_PATH"

    BACKUP_PASSWORD=$(security find-generic-password \
        -a iphone_backup -s iphone_backup_password -w 2>/dev/null) || {
        error "Backup password not in Keychain."
        error "Run: security add-generic-password -a iphone_backup -s iphone_backup_password -w 'YOUR_PASSWORD'"
        return 1
    }
    export BACKUP_PASSWORD

    log "Creating backup (5-10 min)..."
    local err_file="${TEMP_DIR}/backup.stderr"
    if ! idevicebackup2 backup --udid "$DEVICE_UDID" "$BACKUP_PATH" 2> >(tee "$err_file" >&2); then
        diagnose_backup_error "$err_file"
        return 1
    fi
    log "✓ Backup created"
}

decrypt_backup() {
    log "=== PHASE 3: Decrypt & Locate ChatStorage ==="
    EXTRACT_DIR="${TEMP_DIR}/extracted"
    mkdir -p "$EXTRACT_DIR"

    python3 - <<PYEOF
import sys
from pathlib import Path
from iphone_backup_decrypt import EncryptedBackup, RelativePath

backup_dir = Path("$BACKUP_PATH") / "$DEVICE_UDID"
out = Path("$EXTRACT_DIR")
out.mkdir(parents=True, exist_ok=True)

try:
    eb = EncryptedBackup(backup_directory=str(backup_dir), passphrase="$BACKUP_PASSWORD")
    eb.extract_file(relative_path=RelativePath.WHATSAPP_MESSAGES,
                    output_filename=str(out / "ChatStorage.sqlite"))
    eb.extract_files(domain_like="AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
                     output_folder=str(out / "media"))
except Exception as e:
    msg = str(e).lower()
    if "password" in msg or "passphrase" in msg or "decrypt" in msg:
        print(f"[ERROR] Decryption failed — password is wrong: {e}", file=sys.stderr)
    else:
        print(f"[ERROR] Decryption failed: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

    CHAT_STORAGE="${EXTRACT_DIR}/ChatStorage.sqlite"
    if [[ ! -f "$CHAT_STORAGE" ]]; then
        error "ChatStorage.sqlite not extracted (decryption failed silently?)"
        return 1
    fi
    log "✓ ChatStorage.sqlite ready"
}

extract_and_export() {
    log "=== PHASE 4: Extract Messages (mode=$SYNC_MODE) ==="
    [[ -n "$TARGET_CONTACT" ]] && log "  Target contact: $TARGET_CONTACT"
    [[ "$INCLUDE_SYSTEM" == true ]] && log "  Including system messages"

    EXPORT_FILE="${EXPORTS_DIR}/whatsapp_export_${TIMESTAMP}.json"

    local args=(
        --db "$CHAT_STORAGE"
        --extracted-root "$EXTRACT_DIR"
        --output "$EXPORT_FILE"
        --attachments-dir "$ATTACHMENTS_DIR"
        --state-file "$STATE_FILE"
        --mode "$SYNC_MODE"
    )
    [[ -n "$TARGET_CONTACT" ]] && args+=(--contact "$TARGET_CONTACT")
    [[ "$INCLUDE_SYSTEM" == true ]] && args+=(--include-system)

    if ! python3 "$EXTRACTOR" "${args[@]}"; then
        error "Message extraction failed"
        return 1
    fi
    log "✓ Export: $EXPORT_FILE"
}

validate_export() {
    log "=== PHASE 4.5: Schema Validation ==="
    if [[ ! -f "$VALIDATOR" || ! -f "$SCHEMA_FILE" ]]; then
        warn "Validator or schema missing — skipping validation"
        return 0
    fi
    if python3 "$VALIDATOR" --export "$EXPORT_FILE" --schema "$SCHEMA_FILE"; then
        log "✓ Export validates against schema"
    else
        error "Export does NOT conform to schema. Refusing to rsync."
        return 1
    fi
}

secure_cleanup() {
    log "=== PHASE 5: Secure Cleanup ==="
    find "$TEMP_DIR" -type f \( \
        -name "ChatStorage.sqlite" -o \
        -name "*.plist" -o \
        -name "Manifest.db" \
    \) -exec shred -vfz -n 7 {} \; 2>/dev/null || true
    log "✓ Sensitive files shredded"
}

sync_remote() {
    if [[ "$SKIP_SYNC" == true ]]; then
        log "=== PHASE 6: Remote Sync (SKIPPED) ==="
        return 0
    fi

    log "=== PHASE 6: Remote Sync to Mikoshi ==="
    if [[ -z "${SSH_HOST:-}" || -z "${SSH_USER:-}" || -z "${SSH_PATH:-}" ]]; then
        warn "SSH_HOST/USER/PATH not configured — skipping rsync."
        return 0
    fi

    log "Syncing to $SSH_USER@$SSH_HOST:$SSH_PATH"
    if rsync -avz --checksum \
        -e "ssh -o StrictHostKeyChecking=accept-new" \
        "$EXPORTS_DIR/" \
        "${SSH_USER}@${SSH_HOST}:${SSH_PATH}/"; then
        log "✓ Remote sync OK"
        SYNC_SUCCEEDED=true
    else
        error "rsync failed"
        return 1
    fi
}

# GC: keep last N JSON exports locally, shred the rest. Then drop
# attachments not referenced by any retained JSON.
gc_local_exports() {
    if [[ "${SYNC_SUCCEEDED:-false}" != true ]]; then
        log "=== PHASE 7: GC (SKIPPED — no successful remote sync) ==="
        return 0
    fi

    log "=== PHASE 7: GC old exports (keep last $KEEP_LOCAL_EXPORTS) ==="

    local stale_jsons
    stale_jsons=$(ls -t "$EXPORTS_DIR"/whatsapp_export_*.json 2>/dev/null | tail -n +$((KEEP_LOCAL_EXPORTS + 1)))

    if [[ -z "$stale_jsons" ]]; then
        log "Nothing to GC (≤ $KEEP_LOCAL_EXPORTS exports present)"
        return 0
    fi

    local count
    count=$(echo "$stale_jsons" | wc -l | xargs)
    log "Shredding $count old export(s)"
    echo "$stale_jsons" | xargs -I{} shred -vfz -n 3 {} 2>/dev/null || true

    # Attachment GC: keep only sha256s referenced by retained JSONs
    python3 - <<PYEOF
import json
import os
import subprocess
from pathlib import Path

exports = Path("$EXPORTS_DIR")
attachments = Path("$ATTACHMENTS_DIR")
if not attachments.exists():
    raise SystemExit(0)

referenced = set()
for j in exports.glob("whatsapp_export_*.json"):
    try:
        with open(j) as f:
            data = json.load(f)
    except Exception:
        continue
    for chat in data.get("chats", []):
        for msg in chat.get("messages", []):
            att = msg.get("attachment")
            if att and not att.get("skipped") and att.get("filename"):
                referenced.add(att["filename"])

removed = 0
for f in attachments.iterdir():
    if f.is_file() and f.name not in referenced:
        subprocess.run(["shred", "-vfz", "-n", "3", str(f)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass
        removed += 1
print(f"[INFO] GC: removed {removed} unreferenced attachment(s)")
PYEOF

    log "✓ GC complete"
}

main() {
    SYNC_SUCCEEDED=false

    log "╔════════════════════════════════════════════════════╗"
    log "║   WhatsApp → Mikoshi Pipeline                      ║"
    log "║   Mode: $SYNC_MODE${TARGET_CONTACT:+ (contact: $TARGET_CONTACT)}"
    log "╚════════════════════════════════════════════════════╝"

    acquire_lock
    setup_python_env

    detect_device           || exit 1
    create_backup           || exit 2
    decrypt_backup          || exit 2
    extract_and_export      || exit 2
    validate_export         || exit 2
    secure_cleanup          || exit 1
    sync_remote             || exit 3
    gc_local_exports        || warn "GC failed (non-fatal)"
}

main
