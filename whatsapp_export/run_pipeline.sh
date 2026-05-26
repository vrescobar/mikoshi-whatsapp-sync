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
STATE_FILE="${SCRIPT_DIR}/.sync_state.json"
LOCK_FILE="${SCRIPT_DIR}/.pipeline.lock"
CONFIG_FILE="${HOME}/.whatsapp_export.conf"                      # legacy rsync
INGEST_CONF="${MIKOSHI_INGEST_CONF:-${HOME}/.mikoshi-ingest.conf}"   # HTTP push + shared pipeline env
EXTRACTOR="${SCRIPT_DIR}/extract_messages.py"
VALIDATOR="${SCRIPT_DIR}/validate_export.py"
SCHEMA_FILE="${SCRIPT_DIR}/schema.json"

# Load ~/.mikoshi-ingest.conf early so env vars defined there (MIKOSHI_BACKUP_DIR,
# MIKOSHI_CLIENT_ID, KEEP_LOCAL_EXPORTS, ...) take effect before we compute paths.
# Lines must be KEY=VALUE (no `export` needed — set -a auto-exports).
# Precedence: env vars set by the user > values from the file.
if [[ -f "$INGEST_CONF" ]]; then
    _saved_MIKOSHI_URL="${MIKOSHI_URL:-}"
    _saved_MIKOSHI_TOKEN="${MIKOSHI_TOKEN:-}"
    _saved_MIKOSHI_BACKUP_DIR="${MIKOSHI_BACKUP_DIR:-}"
    _saved_MIKOSHI_CLIENT_ID="${MIKOSHI_CLIENT_ID:-}"
    _saved_KEEP_LOCAL_EXPORTS="${KEEP_LOCAL_EXPORTS:-}"
    _saved_MIKOSHI_FAVORITES_FILE="${MIKOSHI_FAVORITES_FILE:-}"

    set -a
    # shellcheck disable=SC1090
    source "$INGEST_CONF"
    set +a

    [[ -n "$_saved_MIKOSHI_URL" ]] && export MIKOSHI_URL="$_saved_MIKOSHI_URL"
    [[ -n "$_saved_MIKOSHI_TOKEN" ]] && export MIKOSHI_TOKEN="$_saved_MIKOSHI_TOKEN"
    [[ -n "$_saved_MIKOSHI_BACKUP_DIR" ]] && export MIKOSHI_BACKUP_DIR="$_saved_MIKOSHI_BACKUP_DIR"
    [[ -n "$_saved_MIKOSHI_CLIENT_ID" ]] && export MIKOSHI_CLIENT_ID="$_saved_MIKOSHI_CLIENT_ID"
    [[ -n "$_saved_KEEP_LOCAL_EXPORTS" ]] && export KEEP_LOCAL_EXPORTS="$_saved_KEEP_LOCAL_EXPORTS"
    [[ -n "$_saved_MIKOSHI_FAVORITES_FILE" ]] && export MIKOSHI_FAVORITES_FILE="$_saved_MIKOSHI_FAVORITES_FILE"
fi

# Temp / backup location. Override with MIKOSHI_BACKUP_DIR to use an external
# disk (recommended when the iPhone backup is larger than your Mac's free space).
# When set, only sensitive files are shredded — the directory itself is left
# alone (you may have other backups there).
TEMP_DIR="${MIKOSHI_BACKUP_DIR:-${SCRIPT_DIR}/temp}"
TEMP_DIR_IS_EXTERNAL=false
[[ -n "${MIKOSHI_BACKUP_DIR:-}" ]] && TEMP_DIR_IS_EXTERNAL=true

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PIPELINE_LOG="${LOG_DIR}/pipeline_${TIMESTAMP}.log"

SYNC_MODE="incremental"
TARGET_CONTACT=""
TARGET_CHAT_JID=""
SINCE=""
SKIP_SYNC=false
INCLUDE_SYSTEM=false
KEEP_LOCAL_EXPORTS="${KEEP_LOCAL_EXPORTS:-5}"
FAVORITES_FILE=""
USE_FAVORITES=false
FROM_PHASE=1

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

Options:
  --mode <incremental|full|full-contact>
        Default: incremental.
  --contact <name-or-jid>
        Required when --mode=full-contact. Substring match against
        ZPARTNERNAME / ZCONTACTJID — use --chat-jid for an exact match.
  --chat-jid <jid>
        Restrict the run to messages of this exact ZCONTACTJID. When set,
        Phase 3 ALSO switches to selective decryption: only ChatStorage.sqlite
        plus the media attachments belonging to this chat are decrypted,
        instead of the whole WhatsApp shared domain. Massive speedup when
        you only care about one DM.
  --since <YYYY-MM-DD>
        Only extract messages whose timestamp is >= this date. Combines with
        the per-chat incremental cursor: never rewinds, never goes earlier.
  --include-system
        Include WhatsApp system messages (group events, encryption notices).
  --skip-remote-sync
        Run extraction but don't rsync to Mikoshi server.
  --keep-local <N>
        Override KEEP_LOCAL_EXPORTS (default 5). Older exports are shredded
        after successful remote sync.
  --favorites [PATH]
        Restrict extraction to JIDs listed in the favorites file. Default
        path: \$MIKOSHI_FAVORITES_FILE or ~/.mikoshi-favorites.json.
        Errors out if the file is missing/empty.
  --from-phase N
        Skip earlier phases. N ∈ {1..6}. Useful after a Phase-4 failure:
        rerun with --from-phase 3 to reuse the encrypted backup (no new
        backup made, no iPhone needed). Requires that the artifacts each
        skipped phase would normally produce already exist:
          --from-phase 3 → needs MIKOSHI_BACKUP_DIR/backup/<UDID>/
          --from-phase 4 → also needs MIKOSHI_BACKUP_DIR/extracted/ChatStorage.sqlite
  --help, -h
        Show this message.

Environment variables:
  MIKOSHI_BACKUP_DIR
        Override location of the iPhone backup + decryption workspace.
        Use this when your Mac doesn't have enough free space to hold the
        whole iPhone backup. The encrypted backup is preserved between runs
        so incremental backups are fast; only decrypted artifacts are wiped.
        Example: export MIKOSHI_BACKUP_DIR=/Volumes/ExternalSSD/iphone_backup
  KEEP_LOCAL_EXPORTS
        See --keep-local.
  MIKOSHI_CLIENT_ID
        Override the hostname recorded in each export.
  MIKOSHI_URL / MIKOSHI_TOKEN
        Mikoshi REST ingest credentials (or put them in ~/.mikoshi-ingest.conf).

Examples:
  $(basename "$0")
  $(basename "$0") --mode full
  $(basename "$0") --mode full-contact --contact "Alice"
  $(basename "$0") --include-system

  # Backup to external SSD (recommended if your Mac is tight on disk):
  export MIKOSHI_BACKUP_DIR=/Volumes/ExternalSSD/iphone_backup
  $(basename "$0") --mode full-contact --contact "Alice" --skip-remote-sync
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode) SYNC_MODE="$2"; shift 2 ;;
        --contact) TARGET_CONTACT="$2"; shift 2 ;;
        --chat-jid) TARGET_CHAT_JID="$2"; shift 2 ;;
        --since) SINCE="$2"; shift 2 ;;
        --include-system) INCLUDE_SYSTEM=true; shift ;;
        --skip-remote-sync) SKIP_SYNC=true; shift ;;
        --keep-local) KEEP_LOCAL_EXPORTS="$2"; shift 2 ;;
        --favorites)
            USE_FAVORITES=true
            # Optional inline path: --favorites /custom/path
            if [[ $# -ge 2 && "$2" != --* ]]; then
                FAVORITES_FILE="$2"; shift 2
            else
                shift
            fi
            ;;
        --from-phase)
            FROM_PHASE="$2"; shift 2
            if ! [[ "$FROM_PHASE" =~ ^[1-6]$ ]]; then
                echo "ERROR: --from-phase must be an integer 1..6 (got: $FROM_PHASE)"
                exit 1
            fi
            ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ "$SYNC_MODE" == "full-contact" && -z "$TARGET_CONTACT" ]]; then
    echo "ERROR: --mode full-contact requires --contact"
    exit 1
fi

if [[ -n "$TARGET_CHAT_JID" && -n "$TARGET_CONTACT" ]]; then
    echo "ERROR: --chat-jid and --contact are mutually exclusive"
    exit 1
fi

if [[ "$USE_FAVORITES" == true ]]; then
    : "${FAVORITES_FILE:=${MIKOSHI_FAVORITES_FILE:-${HOME}/.mikoshi-favorites.json}}"
    if [[ ! -f "$FAVORITES_FILE" ]]; then
        echo "ERROR: --favorites requested but file not found: $FAVORITES_FILE"
        exit 1
    fi
fi

mkdir -p "$LOG_DIR" "$EXPORTS_DIR" "$ATTACHMENTS_DIR"

# For external backup dir, check the mount actually exists (avoid silently
# writing to a stale path if the drive isn't plugged in).
if [[ "$TEMP_DIR_IS_EXTERNAL" == true ]]; then
    parent="$(dirname "$TEMP_DIR")"
    if [[ ! -d "$parent" ]]; then
        echo "ERROR: MIKOSHI_BACKUP_DIR parent does not exist: $parent"
        echo "       Is the external drive plugged in and mounted?"
        exit 1
    fi
fi
mkdir -p "$TEMP_DIR"

exec > >(tee -a "$PIPELINE_LOG") 2>&1

log()   { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"; }
error() { echo -e "${RED}[ERROR $(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" >&2; }
warn()  { echo -e "${YELLOW}[WARN $(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"; }
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }

if [[ "$TEMP_DIR_IS_EXTERNAL" == true ]]; then
    log "Using external backup dir: $TEMP_DIR"
    free_gb=$(df -g "$TEMP_DIR" | awk 'NR==2 {print $4}')
    log "  Free space: ${free_gb} GB"
    if [[ -n "$free_gb" && "$free_gb" -lt 50 ]]; then
        warn "  Less than 50 GB free — iPhone backups can be huge. Continue at your own risk."
    fi
fi

# Report on the actual config that matters for the modern HTTP push flow.
if [[ -f "$INGEST_CONF" ]]; then
    log "Mikoshi config loaded from $INGEST_CONF"
    if [[ -z "${MIKOSHI_URL:-}" ]]; then
        warn "MIKOSHI_URL is empty — push to Mikoshi will be skipped."
    fi
    if [[ -z "${MIKOSHI_TOKEN:-}" ]]; then
        warn "MIKOSHI_TOKEN is empty — push to Mikoshi will be skipped."
    fi
else
    warn "$INGEST_CONF not found. Push to Mikoshi will be skipped (extraction still runs)."
    warn "To enable push, create the file with: MIKOSHI_URL=... and MIKOSHI_TOKEN=..."
fi

# Legacy SSH/rsync config — kept for back-compat only. Silently source if
# present; don't warn when absent (it's expected to be missing on new installs).
if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
    log "Legacy SSH config also loaded from $CONFIG_FILE"
fi

cleanup() {
    local exit_code=$?
    log "=== Cleanup (exit $exit_code) ==="

    # When the pipeline fails AFTER Phase 3 (decrypt succeeded) but before
    # Phase 5 (own cleanup), preserving extracted/ lets the user iterate
    # with `--from-phase 4` (or 5/6) instead of paying ~30 min for a fresh
    # decrypt. Safe because extracted/ never contains the encrypted backup.
    # Set MIKOSHI_PRESERVE_EXTRACTED=0 to opt out.
    local preserve_extracted=false
    if [[ $exit_code -ne 0 \
        && "${MIKOSHI_PRESERVE_EXTRACTED:-1}" != "0" \
        && "$TEMP_DIR_IS_EXTERNAL" == true \
        && -f "${TEMP_DIR}/extracted/ChatStorage.sqlite" ]]; then
        preserve_extracted=true
    fi

    if [[ -d "$TEMP_DIR" ]]; then
        # CRITICAL: the shred target MUST be restricted to the decrypted
        # working area, NEVER the whole $TEMP_DIR. When MIKOSHI_BACKUP_DIR
        # is used as $TEMP_DIR, recursing into it would match Manifest.plist /
        # Status.plist / Info.plist / Manifest.db inside the *encrypted*
        # iPhone backup tree (those files have the same names as our
        # decrypted artifacts) and destroy them — irrecoverably, with the
        # 7-pass shred. This bug cost a 55h backup once.
        #
        # The decrypted artifacts only ever live in $TEMP_DIR/extracted/.
        # Anything in $TEMP_DIR/backup/<UDID>/ is the iPhone's encrypted
        # backup and must remain untouched.
        if [[ -d "${TEMP_DIR}/extracted" && "$preserve_extracted" != true ]]; then
            find "${TEMP_DIR}/extracted" -type f \( \
                -name "ChatStorage.sqlite" -o \
                -name "*.plist" -o \
                -name "Manifest.db" -o \
                -name "Status" \
            \) -exec shred -vfz -n 7 {} \; 2>/dev/null || true
        fi

        if [[ "$preserve_extracted" == true ]]; then
            log "✓ Keeping extracted/ for iteration (pipeline failed at exit $exit_code)"
            log "  Re-run with: ./mikoshi-whatsapp.sh sync --all --from-phase 4"
            rm -f "${TEMP_DIR}/backup.stderr"
        elif [[ "$TEMP_DIR_IS_EXTERNAL" == true ]]; then
            # External backup dir (MIKOSHI_BACKUP_DIR): preserve the encrypted
            # iPhone backup so future incremental backups are fast. Only nuke
            # the per-run extracted/ subdir which contains decrypted data.
            rm -rf "${TEMP_DIR}/extracted"
            rm -f "${TEMP_DIR}/backup.stderr"
            log "✓ Decrypted artifacts cleaned (encrypted backup kept in $TEMP_DIR)"
        else
            rm -rf "$TEMP_DIR"
            log "✓ Temp cleaned"
        fi
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

# Map idevicebackup2 output patterns to actionable messages.
# Patterns are ordered from most-specific to most-generic.
diagnose_backup_error() {
    local err_file="$1"
    [[ -f "$err_file" ]] || return

    # Most specific first: backup dir has corrupt metadata from a prior failed run
    if grep -qiE "deserializing property list|Error reading status|Could not read Info\\.plist|ErrorCode 205" "$err_file"; then
        error "Backup directory has CORRUPT metadata from a previous failed attempt."
        error "Symptoms in idevicebackup2 output: 'Status.plist' / 'Info.plist' deserialization errors."
        error ""
        error "Fix: wipe the partial backup so a fresh one can start:"
        if [[ "$TEMP_DIR_IS_EXTERNAL" == true ]]; then
            error "  ./mikoshi-whatsapp.sh reset-backup"
            error "  (this deletes ${TEMP_DIR}/backup/<UDID>/ only — the rest of MIKOSHI_BACKUP_DIR is untouched)"
        else
            error "  rm -rf ${TEMP_DIR}/backup"
        fi
        return
    fi
    if grep -qiE "MBErrorDomain/104|MBErrorDomain/106|backup is encrypted with a different password" "$err_file"; then
        error "Backup password mismatch: this device's backup was created with a different password."
        error "Either delete the backup dir, or update Keychain to the right one."
        return
    fi
    if grep -qiE "wrong password|incorrect password" "$err_file"; then
        error "Backup password rejected by device."
        error "Either the password in Keychain is wrong, or you changed it on the iPhone."
        error "Fix: security delete-generic-password -a iphone_backup -s iphone_backup_password"
        error "     security add-generic-password -a iphone_backup -s iphone_backup_password -w 'NEW_PASSWORD'"
        return
    fi
    if grep -qiE "device is locked|passcode protected" "$err_file"; then
        error "iPhone is locked. Unlock the device and re-run."
        return
    fi
    if grep -qiE "trust this computer|pairing.*fail|not paired" "$err_file"; then
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
        # Use awk + sed to trim — xargs chokes on names with apostrophes/quotes
        DEVICE_NAME=$(ideviceinfo -u "$DEVICE_UDID" | awk -F': ' '/^DeviceName:/ {print $2}' | sed 's/[[:space:]]*$//')
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

    log "Creating backup (first run can be hours; subsequent are incremental)..."
    local err_file="${TEMP_DIR}/backup.stderr"
    # Full output log — backup_progress.py mirrors all idevicebackup2 lines
    # here via MIKOSHI_BACKUP_LOG so diagnose_backup_error has something to
    # grep. (Plain stderr capture isn't enough since backup_progress.py owns
    # the TTY for Rich rendering.)
    local backup_log="${TEMP_DIR}/backup.log"

    # Use the rich progress wrapper unless explicitly disabled.
    # Falls back to plain idevicebackup2 if rich isn't importable.
    local backup_cmd=(idevicebackup2 backup --udid "$DEVICE_UDID" "$BACKUP_PATH")
    if [[ "${MIKOSHI_PLAIN_PROGRESS:-0}" != "1" ]] && \
       python3 -c "import rich" 2>/dev/null; then
        backup_cmd=(python3 "${SCRIPT_DIR}/backup_progress.py" --udid "$DEVICE_UDID" "$BACKUP_PATH")
    fi

    if ! MIKOSHI_BACKUP_LOG="$backup_log" "${backup_cmd[@]}" 2> >(tee "$err_file" >&2); then
        # Diagnose against the full log (preferred) or stderr (fallback).
        if [[ -s "$backup_log" ]]; then
            diagnose_backup_error "$backup_log"
        else
            diagnose_backup_error "$err_file"
        fi
        return 1
    fi
    log "✓ Backup created"
}

decrypt_backup() {
    log "=== PHASE 3: Decrypt & Locate ChatStorage ==="
    EXTRACT_DIR="${TEMP_DIR}/extracted"
    mkdir -p "$EXTRACT_DIR"

    # Both decrypt paths (3A whole-domain and 3B chat-only) go through
    # selective_decrypt.py — the bash side just chooses the args. Keeping
    # the Python logic in a module means it's unit-testable; embedded
    # heredocs aren't.
    local sel_args=(
        --backup-dir "${BACKUP_PATH}/${DEVICE_UDID}"
        --out-dir "$EXTRACT_DIR"
    )
    if [[ -n "$TARGET_CHAT_JID" ]]; then
        log "  Selective decrypt: chat $TARGET_CHAT_JID"
        sel_args+=(--chat-jid "$TARGET_CHAT_JID")
    else
        log "  Decrypting WhatsApp shared domain (full)"
    fi

    if ! BACKUP_PASSWORD="$BACKUP_PASSWORD" \
        python3 "${SCRIPT_DIR}/selective_decrypt.py" "${sel_args[@]}"; then
        error "Decryption failed"
        return 1
    fi

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
    [[ -n "$TARGET_CHAT_JID" ]] && args+=(--chat-jid "$TARGET_CHAT_JID")
    [[ -n "$SINCE" ]] && args+=(--since "$SINCE")
    [[ "$INCLUDE_SYSTEM" == true ]] && args+=(--include-system)
    if [[ "$USE_FAVORITES" == true ]]; then
        args+=(--favorites-file "$FAVORITES_FILE")
        log "  Favorites file: $FAVORITES_FILE"
    fi

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
    # Scoped to $TEMP_DIR/extracted/ ONLY — see cleanup() comment for why.
    # Recursing $TEMP_DIR with MIKOSHI_BACKUP_DIR active would destroy the
    # encrypted iPhone backup.
    if [[ -d "${TEMP_DIR}/extracted" ]]; then
        find "${TEMP_DIR}/extracted" -type f \( \
            -name "ChatStorage.sqlite" -o \
            -name "*.plist" -o \
            -name "Manifest.db" \
        \) -exec shred -vfz -n 7 {} \; 2>/dev/null || true
    fi
    log "✓ Sensitive files shredded"
}

sync_remote() {
    if [[ "$SKIP_SYNC" == true ]]; then
        log "=== PHASE 6: Mikoshi REST push (SKIPPED) ==="
        return 0
    fi

    log "=== PHASE 6: Mikoshi REST push ==="

    # Config: ~/.mikoshi-ingest.conf or env. Required: MIKOSHI_URL, MIKOSHI_TOKEN.
    if [[ -z "${MIKOSHI_URL:-}" || -z "${MIKOSHI_TOKEN:-}" ]]; then
        local conf="${HOME}/.mikoshi-ingest.conf"
        if [[ -f "$conf" ]]; then
            # shellcheck disable=SC1090
            source "$conf"
        fi
    fi
    if [[ -z "${MIKOSHI_URL:-}" || -z "${MIKOSHI_TOKEN:-}" ]]; then
        warn "MIKOSHI_URL / MIKOSHI_TOKEN not configured — skipping push."
        return 0
    fi

    # Manifest is the most-recent export JSON written this run.
    local manifest
    manifest=$(ls -t "$EXPORTS_DIR"/whatsapp_export_*.json 2>/dev/null | head -1)
    if [[ -z "$manifest" || ! -f "$manifest" ]]; then
        error "No manifest found in $EXPORTS_DIR"
        return 1
    fi

    log "Pushing $manifest to $MIKOSHI_URL"
    if MIKOSHI_URL="$MIKOSHI_URL" MIKOSHI_TOKEN="$MIKOSHI_TOKEN" \
        python3 "$SCRIPT_DIR/push_via_api.py" \
            --manifest "$manifest" \
            --attachments-dir "$ATTACHMENTS_DIR"; then
        log "✓ Mikoshi push OK"
        SYNC_SUCCEEDED=true
    else
        error "Mikoshi push failed"
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
    [[ "$FROM_PHASE" -gt 1 ]] && log "║   Starting from Phase $FROM_PHASE (skipping prior phases)"
    log "╚════════════════════════════════════════════════════╝"

    acquire_lock
    setup_python_env

    # When skipping phases, we still need the path variables that earlier
    # phases would have set. Reconstruct them from the existing on-disk state.
    if [[ "$FROM_PHASE" -gt 1 ]]; then
        if [[ "$TEMP_DIR_IS_EXTERNAL" != true ]]; then
            error "--from-phase requires MIKOSHI_BACKUP_DIR (external backup dir)."
            error "Without it, prior phases' artifacts would have been wiped."
            exit 1
        fi
        BACKUP_PATH="${TEMP_DIR}/backup"
        # Derive UDID from the backup tree
        if [[ "$FROM_PHASE" -ge 3 ]]; then
            local _udids=("$BACKUP_PATH"/*)
            if [[ ! -d "${_udids[0]:-}" ]]; then
                error "--from-phase $FROM_PHASE needs $BACKUP_PATH/<UDID>/ to exist."
                exit 1
            fi
            DEVICE_UDID=$(basename "${_udids[0]}")
            log "  Reusing encrypted backup at $BACKUP_PATH/$DEVICE_UDID"
            # Phase 2 normally exports BACKUP_PASSWORD for Phase 3. Re-fetch it.
            BACKUP_PASSWORD=$(security find-generic-password \
                -a iphone_backup -s iphone_backup_password -w 2>/dev/null) || {
                error "Backup password not in Keychain (needed by Phase 3)."
                exit 1
            }
            export BACKUP_PASSWORD
        fi
        if [[ "$FROM_PHASE" -ge 4 ]]; then
            EXTRACT_DIR="${TEMP_DIR}/extracted"
            CHAT_STORAGE="${EXTRACT_DIR}/ChatStorage.sqlite"
            if [[ ! -f "$CHAT_STORAGE" ]]; then
                error "--from-phase $FROM_PHASE needs $CHAT_STORAGE to exist."
                exit 1
            fi
            log "  Reusing decrypted ChatStorage at $CHAT_STORAGE"
        fi
    fi

    [[ "$FROM_PHASE" -le 1 ]] && { detect_device       || exit 1; }
    [[ "$FROM_PHASE" -le 2 ]] && { create_backup       || exit 2; }
    [[ "$FROM_PHASE" -le 3 ]] && { decrypt_backup      || exit 2; }
    [[ "$FROM_PHASE" -le 4 ]] && { extract_and_export  || exit 2; }
                                  validate_export      || exit 2
    [[ "$FROM_PHASE" -le 5 ]] && { secure_cleanup      || exit 1; }
    [[ "$FROM_PHASE" -le 6 ]] && { sync_remote         || exit 3; }
                                  gc_local_exports     || warn "GC failed (non-fatal)"
}

main
