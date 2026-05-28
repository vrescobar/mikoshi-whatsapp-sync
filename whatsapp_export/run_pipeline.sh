#!/bin/bash
#
# WhatsApp → Mikoshi pipeline (post-redesign).
#
# Phase order (see REDESIGN.md §6):
#   1. prepare       — detect device (or detect cached backup as fallback)
#   2. acquire       — make/refresh encrypted backup via idevicebackup2
#   3. decrypt-db    — selective_decrypt → extracted/ChatStorage.sqlite ONLY (~10s)
#   3b. decrypt-media— scoped media decrypt based on TARGET_CHAT_JID / favorites / all
#   4. extract       — extract_messages.py → exports/whatsapp_export_*.json
#   4.5 validate     — validate_export.py against schema.json
#   5. push&confirm  — push_via_api.py → manifest / media / commit
#                       cursor cache is updated INSIDE this step on commit 200
#   6. gc            — keep last KEEP_LOCAL_EXPORTS json exports + their attachments
#
# Cursor advancement:
#   .sync_state.json is now a *cache* of the server's per-chat cursors,
#   written exclusively by push_via_api after a 200 from /commit.
#   extract_messages.py no longer writes the file in default mode —
#   set MIKOSHI_TRUST_LOCAL_CURSOR=1 for the legacy behaviour.
#
# Secure shred of decrypted artifacts:
#   The pre-redesign Phase 5 (secure_cleanup) was a no-op by default
#   (MIKOSHI_PRESERVE_EXTRACTED defaults to true). It's been removed
#   from the default flow. To shred decrypted artifacts after a run,
#   set MIKOSHI_SECURE_CLEANUP=1 or invoke `./mikoshi-whatsapp.sh purge-extracted`.

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
INGEST_CONF="${MIKOSHI_INGEST_CONF:-${HOME}/.mikoshi-ingest.conf}"
EXTRACTOR="${SCRIPT_DIR}/extract_messages.py"
VALIDATOR="${SCRIPT_DIR}/validate_export.py"
SCHEMA_FILE="${SCRIPT_DIR}/schema.json"

# Pairing timeout for the iPhone trust prompt (Phase 1). Anything beyond
# this is treated as "device unreachable" and the pipeline either falls
# back to a cached backup (when --from-phase ≥ 3) or aborts cleanly.
MIKOSHI_DEVICE_TIMEOUT="${MIKOSHI_DEVICE_TIMEOUT:-300}"

# Load ~/.mikoshi-ingest.conf early so env vars defined there (MIKOSHI_BACKUP_DIR,
# MIKOSHI_CLIENT_ID, KEEP_LOCAL_EXPORTS, ...) take effect before we compute paths.
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
        Required when --mode=full-contact. Substring match — use --chat-jid for exact.
  --chat-jid <jid>
        Restrict the run to messages of this exact ZCONTACTJID. Phase 3b
        switches to per-chat selective decryption automatically.
  --since <YYYY-MM-DD>
        Lift the lower bound for fresh chats. Never rewinds a cursor.
  --include-system
        Include WhatsApp system messages (group events, encryption notices).
  --skip-remote-sync
        Run extraction but don't push to Mikoshi. The cursor cache is NOT
        advanced when push is skipped — there is no commit, so there's no
        confirmation to record.
  --keep-local <N>
        Override KEEP_LOCAL_EXPORTS (default 5).
  --favorites [PATH]
        Restrict extraction (and Phase 3b media decrypt) to JIDs listed
        in the favorites file. Default path: \$MIKOSHI_FAVORITES_FILE
        or ~/.mikoshi-favorites.json.
  --from-phase N
        Skip earlier phases. N ∈ {1..6}. Useful after a failure — see
        REDESIGN.md §6 for phase semantics.
  --help, -h
        Show this message.

Environment:
  MIKOSHI_BACKUP_DIR             External backup workspace.
  MIKOSHI_DEVICE_TIMEOUT         Phase 1 trust-prompt timeout in seconds (default: 300).
  MIKOSHI_SECURE_CLEANUP         Set to 1 to shred decrypted artifacts after a successful run.
  MIKOSHI_TRUST_LOCAL_CURSOR     Legacy: re-enable extraction-time cursor writes (re-introduces drift bug).
  MIKOSHI_URL / MIKOSHI_TOKEN    Mikoshi REST ingest credentials.

Examples:
  $(basename "$0")
  $(basename "$0") --mode full
  $(basename "$0") --favorites
  $(basename "$0") --chat-jid '34xxxxxxxxx@s.whatsapp.net' --since 2026-01-01
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

# ─── lock with PID liveness check ────────────────────────────────────────
#
# The pre-redesign acquire_lock() wrote $$ to .pipeline.lock and never
# checked whether the PID was alive. A kill -9 mid-run bricked every
# subsequent run (notably 6h cron) until someone manually rm'd the file.
# Now we re-acquire the lock if its PID is no longer running and the file
# is older than 1 hour (safe heuristic — backups can take 90 minutes).
acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local lock_pid
        lock_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
        if [[ -n "$lock_pid" ]] && kill -0 "$lock_pid" 2>/dev/null; then
            error "Pipeline already running (lock: $LOCK_FILE, PID $lock_pid)"
            error "If you're sure that's stale: rm $LOCK_FILE"
            exit 1
        fi
        # Stale lock — owner is gone.
        warn "Stale lock detected (PID ${lock_pid:-?} no longer running). Reclaiming."
        rm -f "$LOCK_FILE"
    fi
    echo $$ > "$LOCK_FILE"
}

# ─── cleanup trap (no longer shreds by default) ──────────────────────────
#
# Pre-redesign this also acted as the secure-cleanup phase (shredding
# decrypted artifacts on every successful run). That's now opt-in via
# MIKOSHI_SECURE_CLEANUP=1 — see secure_cleanup_optin().
cleanup() {
    local exit_code=$?

    if [[ -f "${TEMP_DIR}/backup.stderr" ]]; then
        rm -f "${TEMP_DIR}/backup.stderr"
    fi

    # Opt-in shred on success only.
    if [[ $exit_code -eq 0 && "${MIKOSHI_SECURE_CLEANUP:-0}" == "1" ]]; then
        secure_cleanup_optin
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

# ─── error-message decoder for idevicebackup2 ────────────────────────────
diagnose_backup_error() {
    local err_file="$1"
    [[ -f "$err_file" ]] || return

    if grep -qiE "deserializing property list|Error reading status|Could not read Info\\.plist|ErrorCode 205" "$err_file"; then
        error "Backup directory has CORRUPT metadata from a previous failed attempt."
        error "Fix: wipe the partial backup so a fresh one can start:"
        if [[ "$TEMP_DIR_IS_EXTERNAL" == true ]]; then
            error "  ./mikoshi-whatsapp.sh reset-backup"
        else
            error "  rm -rf ${TEMP_DIR}/backup"
        fi
        return
    fi
    if grep -qiE "MBErrorDomain/104|MBErrorDomain/106|backup is encrypted with a different password" "$err_file"; then
        error "Backup password mismatch: this device's backup was created with a different password."
        return
    fi
    if grep -qiE "wrong password|incorrect password" "$err_file"; then
        error "Backup password rejected by device."
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

# ─── portable bounded wait (bash 3.2 + macOS, no `timeout` dependency) ────
#
# Some macs don't have `timeout(1)` (coreutils not installed). idevice
# binaries occasionally hang when the trust prompt is pending or the
# device is locked. We wrap the call: fork the child, fork a sleeper
# that SIGTERMs it, wait for the child, then kill the sleeper.
run_with_timeout() {
    local secs=$1; shift
    "$@" &
    local pid=$!
    ( sleep "$secs" && kill -TERM "$pid" 2>/dev/null ) &
    local timer=$!
    local rc=0
    wait "$pid" 2>/dev/null || rc=$?
    kill -KILL "$timer" 2>/dev/null || true
    wait "$timer" 2>/dev/null || true
    return $rc
}

# ─── PHASE 1: prepare (formerly detect_device) ───────────────────────────
detect_device() {
    log "=== PHASE 1: Device Detection ==="
    if ! command -v idevice_id &>/dev/null; then
        error "idevice_id not found. Run: bash setup.sh"
        return 1
    fi
    DEVICE_UDID=$(idevice_id -l 2>/dev/null | head -n1 || true)
    if [[ -z "$DEVICE_UDID" ]]; then
        error "No iPhone detected."
        error "  1. Unlock iPhone"
        error "  2. Same WiFi as Mac (or USB)"
        error "  3. WiFi Sync enabled: Settings → General → AirDrop & Handoff → WiFi Sync"
        return 1
    fi
    log "✓ iPhone: $DEVICE_UDID"

    # Trust prompt can hang ideviceinfo forever. Bound it.
    local info_file="${TEMP_DIR}/_ideviceinfo.tmp"
    rm -f "$info_file"
    if run_with_timeout "$MIKOSHI_DEVICE_TIMEOUT" \
        bash -c "ideviceinfo -u '$DEVICE_UDID' > '$info_file' 2>&1"; then
        DEVICE_NAME=$(awk -F': ' '/^DeviceName:/ {print $2}' "$info_file" | sed 's/[[:space:]]*$//' || true)
        log "✓ Device: ${DEVICE_NAME:-(unnamed)}"
    else
        error "Cannot communicate with device within ${MIKOSHI_DEVICE_TIMEOUT}s."
        error "  - iPhone may be locked, untrusted, or off your WiFi."
        error "  - Bump MIKOSHI_DEVICE_TIMEOUT to wait longer."
        return 1
    fi
    rm -f "$info_file"
}

# ─── PHASE 2: acquire (encrypted backup) ─────────────────────────────────
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
    local backup_log="${TEMP_DIR}/backup.log"

    local backup_cmd=(idevicebackup2 backup --udid "$DEVICE_UDID" "$BACKUP_PATH")
    if [[ "${MIKOSHI_PLAIN_PROGRESS:-0}" != "1" ]] && \
       python3 -c "import rich" 2>/dev/null; then
        backup_cmd=(python3 "${SCRIPT_DIR}/backup_progress.py" --udid "$DEVICE_UDID" "$BACKUP_PATH")
    fi

    if ! MIKOSHI_BACKUP_LOG="$backup_log" "${backup_cmd[@]}" 2> >(tee "$err_file" >&2); then
        if [[ -s "$backup_log" ]]; then
            diagnose_backup_error "$backup_log"
        else
            diagnose_backup_error "$err_file"
        fi
        return 1
    fi
    log "✓ Backup created"
}

# ─── PHASE 3a: decrypt-db only (ChatStorage.sqlite, ~10s) ────────────────
decrypt_db() {
    log "=== PHASE 3a: Decrypt ChatStorage.sqlite ==="
    EXTRACT_DIR="${TEMP_DIR}/extracted"
    mkdir -p "$EXTRACT_DIR"

    if ! BACKUP_PASSWORD="$BACKUP_PASSWORD" \
        python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
from pathlib import Path
import os
import selective_decrypt
out = selective_decrypt.decrypt_db_only(
    backup_dir=Path('${BACKUP_PATH}/${DEVICE_UDID}'),
    password=os.environ['BACKUP_PASSWORD'],
    out_dir=Path('${EXTRACT_DIR}'),
)
print(f'[INFO] DB decrypted: {out}')
"; then
        error "Decryption of ChatStorage.sqlite failed"
        return 1
    fi

    CHAT_STORAGE="${EXTRACT_DIR}/ChatStorage.sqlite"
    if [[ ! -f "$CHAT_STORAGE" ]]; then
        error "ChatStorage.sqlite not extracted"
        return 1
    fi
    log "✓ ChatStorage.sqlite ready"
}

# ─── PHASE 3b: decrypt-media (scoped) ────────────────────────────────────
#
# Scope rules:
#   - TARGET_CHAT_JID set → that chat's media only (selective).
#   - USE_FAVORITES=true → media for every JID in the favorites file.
#   - Otherwise → the whole WhatsApp shared domain (today's default for
#     a full sync).
decrypt_media() {
    log "=== PHASE 3b: Decrypt media (scope: $(media_scope_label)) ==="
    EXTRACT_DIR="${TEMP_DIR}/extracted"

    if [[ -n "$TARGET_CHAT_JID" ]]; then
        local jids=("$TARGET_CHAT_JID")
        decrypt_media_jids "${jids[@]}"
        return $?
    fi

    if [[ "$USE_FAVORITES" == true ]]; then
        # shellcheck disable=SC2207
        local fav_jids=( $(python3 -c "
import json, sys
data = json.load(open('${FAVORITES_FILE}'))
for f in data.get('favorites', []):
    if f.get('jid'):
        print(f['jid'])
") )
        if [[ ${#fav_jids[@]} -eq 0 ]]; then
            warn "Favorites file empty; skipping media decrypt"
            return 0
        fi
        decrypt_media_jids "${fav_jids[@]}"
        return $?
    fi

    # No targeting — decrypt the whole shared domain (today's behaviour).
    if ! BACKUP_PASSWORD="$BACKUP_PASSWORD" \
        python3 "${SCRIPT_DIR}/selective_decrypt.py" \
            --backup-dir "${BACKUP_PATH}/${DEVICE_UDID}" \
            --out-dir "$EXTRACT_DIR"; then
        error "Whole-domain media decrypt failed"
        return 1
    fi
    log "✓ Media decrypted (full WhatsApp shared domain)"
}

media_scope_label() {
    if [[ -n "$TARGET_CHAT_JID" ]]; then
        echo "one chat ($TARGET_CHAT_JID)"
    elif [[ "$USE_FAVORITES" == true ]]; then
        echo "favorites"
    else
        echo "all"
    fi
}

decrypt_media_jids() {
    local jids_csv
    jids_csv=$(IFS=,; echo "$*")
    if ! BACKUP_PASSWORD="$BACKUP_PASSWORD" MIKOSHI_DECRYPT_JIDS="$jids_csv" \
        python3 -c "
import os
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
from pathlib import Path
import selective_decrypt
jids = [j for j in os.environ['MIKOSHI_DECRYPT_JIDS'].split(',') if j]
stats = selective_decrypt.decrypt_media_for_jids(
    backup_dir=Path('${BACKUP_PATH}/${DEVICE_UDID}'),
    password=os.environ['BACKUP_PASSWORD'],
    out_dir=Path('${EXTRACT_DIR}'),
    chatstorage_path=Path('${EXTRACT_DIR}/ChatStorage.sqlite'),
    jids=jids,
)
print(f'[INFO] media decrypt: {stats.media_decrypted} decrypted, '
      f'{stats.media_skipped_cached} cached, {stats.media_total_candidates} candidates')
for e in stats.errors:
    print(f'[WARN] {e}', file=sys.stderr)
"; then
        error "Scoped media decrypt failed"
        return 1
    fi
    log "✓ Media decrypted (scoped)"
}

# ─── PHASE 4: extract messages ───────────────────────────────────────────
extract_and_export() {
    log "=== PHASE 4: Extract Messages (mode=$SYNC_MODE) ==="
    [[ -n "$TARGET_CONTACT" ]] && log "  Target contact: $TARGET_CONTACT"
    [[ "$INCLUDE_SYSTEM" == true ]] && log "  Including system messages"

    EXPORT_FILE="${EXPORTS_DIR}/whatsapp_export_${TIMESTAMP}.json"

    local args=(
        --output "$EXPORT_FILE"
        --attachments-dir "$ATTACHMENTS_DIR"
        --state-file "$STATE_FILE"
        --mode "$SYNC_MODE"
    )
    # When MIKOSHI_SOURCES is set (e.g. "iphone_backup,mac_live"), use
    # the multi-source extractor — it pulls from each source, reconciles
    # by stanza id, and writes one deduped manifest. Otherwise fall back
    # to single-source mode using the decrypted iPhone backup, which is
    # what the cron path has done since the redesign.
    if [[ -n "${MIKOSHI_SOURCES:-}" ]]; then
        args+=(--sources "$MIKOSHI_SOURCES")
        log "  Sources: $MIKOSHI_SOURCES"
    else
        args+=(--db "$CHAT_STORAGE" --extracted-root "$EXTRACT_DIR")
    fi
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
        error "Export does NOT conform to schema. Refusing to push."
        return 1
    fi
}

# ─── opt-in secure cleanup ──────────────────────────────────────────────
#
# Pre-redesign this ran unconditionally and shredded ChatStorage.sqlite on
# every "successful" run, forcing a 13-min re-decrypt on the next iteration.
# It's now off by default. Set MIKOSHI_SECURE_CLEANUP=1 to bring it back —
# or invoke `./mikoshi-whatsapp.sh purge-extracted` as a one-shot.
secure_cleanup_optin() {
    log "=== Optional secure cleanup (MIKOSHI_SECURE_CLEANUP=1) ==="
    if [[ -d "${TEMP_DIR}/extracted" ]]; then
        find "${TEMP_DIR}/extracted" -type f \( \
            -name "ChatStorage.sqlite" -o \
            -name "*.plist" -o \
            -name "Manifest.db" \
        \) -exec shred -vfz -n 7 {} \; 2>/dev/null || true
    fi
    log "✓ Sensitive files shredded (encrypted backup kept intact)"
}

# ─── PHASE 5: push & confirm (the only cursor writer) ────────────────────
sync_remote() {
    if [[ "$SKIP_SYNC" == true ]]; then
        log "=== PHASE 5: Mikoshi REST push (SKIPPED) ==="
        warn "Cursor cache NOT advanced — skip-remote-sync means no server confirmation."
        return 0
    fi

    log "=== PHASE 5: Mikoshi REST push ==="

    if [[ -z "${MIKOSHI_URL:-}" || -z "${MIKOSHI_TOKEN:-}" ]]; then
        local conf="${HOME}/.mikoshi-ingest.conf"
        if [[ -f "$conf" ]]; then
            # shellcheck disable=SC1090
            source "$conf"
        fi
    fi
    if [[ -z "${MIKOSHI_URL:-}" || -z "${MIKOSHI_TOKEN:-}" ]]; then
        warn "MIKOSHI_URL / MIKOSHI_TOKEN not configured — skipping push."
        warn "Cursor cache NOT advanced (no server to confirm with)."
        return 0
    fi

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
            --attachments-dir "$ATTACHMENTS_DIR" \
            --state-file "$STATE_FILE"; then
        log "✓ Mikoshi push OK (cursor cache updated from server response)"
        SYNC_SUCCEEDED=true
    else
        error "Mikoshi push failed — cursor cache NOT updated (this is correct behaviour)"
        return 1
    fi
}

# ─── PHASE 6: GC old exports ────────────────────────────────────────────
gc_local_exports() {
    if [[ "${SYNC_SUCCEEDED:-false}" != true ]]; then
        log "=== PHASE 6: GC (SKIPPED — no successful remote sync) ==="
        return 0
    fi

    log "=== PHASE 6: GC old exports (keep last $KEEP_LOCAL_EXPORTS) ==="

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

    # --from-phase support: reconstruct the path variables that earlier
    # phases would have set, then verify the required artifacts exist.
    #
    # Pain point B in REDESIGN.md §2: this used to require MIKOSHI_BACKUP_DIR.
    # Now it accepts either MIKOSHI_BACKUP_DIR/backup/<UDID>/ or
    # SCRIPT_DIR/temp/backup/<UDID>/ — whichever has the artifacts.
    if [[ "$FROM_PHASE" -gt 1 ]]; then
        BACKUP_PATH="${TEMP_DIR}/backup"
        # Only phase 3 (re-decrypt) needs the encrypted UDID directory
        # and the Keychain password. Phase 4 reads the already-decrypted
        # ChatStorage directly and must not block on the encrypted backup
        # being present — that broke the LaunchAgent path when an external
        # SSD wasn't fully populated at trigger time.
        if [[ "$FROM_PHASE" -eq 3 ]] && \
           ! { [[ -n "${MIKOSHI_SOURCES:-}" ]] && \
               [[ ",${MIKOSHI_SOURCES}," != *",iphone_backup,"* ]]; }; then
            local _udid_dir=""
            if [[ -d "$BACKUP_PATH" ]]; then
                for d in "$BACKUP_PATH"/*; do
                    [[ -d "$d" ]] || continue
                    local _name; _name=$(basename "$d")
                    [[ ${#_name} -gt 20 ]] || continue
                    _udid_dir="$d"
                    break
                done
            fi
            if [[ -z "$_udid_dir" ]]; then
                error "--from-phase $FROM_PHASE needs $BACKUP_PATH/<UDID>/ to exist."
                error "  Looked in: $BACKUP_PATH"
                error "  (Set MIKOSHI_BACKUP_DIR to the external SSD path if your backup lives there.)"
                exit 1
            fi
            DEVICE_UDID=$(basename "$_udid_dir")
            log "  Reusing encrypted backup at $_udid_dir"
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
            # Mac-live-only sync: extract reads from the Catalyst app DB
            # under ~/Library/Group Containers/... — no decrypted iPhone
            # backup is needed and CHAT_STORAGE may legitimately be absent.
            if [[ -n "${MIKOSHI_SOURCES:-}" && ",${MIKOSHI_SOURCES}," != *",iphone_backup,"* ]]; then
                log "  Mac-live-only sync (MIKOSHI_SOURCES=$MIKOSHI_SOURCES) — skipping iPhone ChatStorage check"
            else
                if [[ ! -f "$CHAT_STORAGE" ]]; then
                    error "--from-phase $FROM_PHASE needs $CHAT_STORAGE to exist."
                    exit 1
                fi
                # SQLite header sanity check (a killed Phase 3 leaves a zero-headered
                # file; size > 0 isn't enough).
                if ! python3 -c "
import sys
with open('$CHAT_STORAGE', 'rb') as f:
    sys.exit(0 if f.read(16) == b'SQLite format 3\\x00' else 1)
" 2>/dev/null; then
                    error "$CHAT_STORAGE is corrupt (bad SQLite header) — likely from a killed Phase 3."
                    error "  rm '$CHAT_STORAGE' && ./mikoshi-whatsapp.sh sync --from-phase 3"
                    exit 1
                fi
                log "  Reusing decrypted ChatStorage at $CHAT_STORAGE"
            fi
        fi
    fi

    [[ "$FROM_PHASE" -le 1 ]] && { detect_device       || exit 1; }
    [[ "$FROM_PHASE" -le 2 ]] && { create_backup       || exit 2; }
    [[ "$FROM_PHASE" -le 3 ]] && { decrypt_db          || exit 2; }
    [[ "$FROM_PHASE" -le 3 ]] && { decrypt_media       || exit 2; }
    [[ "$FROM_PHASE" -le 4 ]] && { extract_and_export  || exit 2; }
                                  validate_export      || exit 2
    [[ "$FROM_PHASE" -le 5 ]] && { sync_remote         || exit 3; }
                                  gc_local_exports     || warn "GC failed (non-fatal)"
}

main
