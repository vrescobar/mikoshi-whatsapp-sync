#!/bin/bash
#
# Mikoshi WhatsApp pipeline entrypoint.
#
#   ./mikoshi-whatsapp.sh                # open TUI (default)
#   ./mikoshi-whatsapp.sh tui            # same
#   ./mikoshi-whatsapp.sh sync           # cron-friendly: favorites if present, else incremental all
#   ./mikoshi-whatsapp.sh sync --all     # ignore favorites, sync all chats incrementally
#   ./mikoshi-whatsapp.sh sync --full    # full re-sync (resets state)
#   ./mikoshi-whatsapp.sh sync --skip-remote-sync  # local-only test
#   ./mikoshi-whatsapp.sh status         # show config + state
#   ./mikoshi-whatsapp.sh --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${SCRIPT_DIR}/.venv"
LOG_DIR="${SCRIPT_DIR}/logs"
PIPELINE="${SCRIPT_DIR}/run_pipeline.sh"
TUI="${SCRIPT_DIR}/tui.py"

usage() {
    cat <<EOF
Mikoshi WhatsApp pipeline entrypoint.

Subcommands:
  tui          Open the interactive menu (default if no subcommand given).
  sync [OPTS]  Non-interactive sync. Suitable for cron.
                  --all                Sync all chats (ignore favorites).
                  --full               Full re-sync from scratch.
                  --skip-remote-sync   Run extraction but don't push.
                  Anything else gets forwarded to run_pipeline.sh.
  status       Print pipeline status (config, backup, sync state).
  reset-backup [--force]
               Delete the partial/corrupt iPhone backup so the next sync
               can start fresh. Asks for confirmation unless --force.
               Only touches MIKOSHI_BACKUP_DIR/backup/<UDID>/.
  verify-backup [--level 1-4]
               Run integrity checks against the existing encrypted backup
               without touching it. Level 1=files+magic, 2=status, 3=keybag,
               4=ChatStorage extract (default). Useful before/after a sync.
  -h, --help   This message.

Examples:
  $(basename "$0")                                # interactive TUI
  $(basename "$0") sync                           # favorites if any, else all
  $(basename "$0") sync --all                     # force all-chats incremental
  $(basename "$0") sync --skip-remote-sync        # dry run, no push

Cron example (every 6h):
  0 */6 * * * $SCRIPT_DIR/$(basename "$0") sync >> ~/mikoshi-cron.log 2>&1
EOF
}

activate_venv() {
    if [[ -z "${VIRTUAL_ENV:-}" ]]; then
        if [[ -d "$VENV" ]]; then
            # shellcheck disable=SC1091
            source "${VENV}/bin/activate"
        else
            echo "ERROR: Python venv missing at $VENV"
            echo "Run: bash setup.sh"
            exit 1
        fi
    fi
}

# Load ~/.mikoshi-ingest.conf so child processes (tui.py, run_pipeline.sh,
# explore_backup.py, ...) all inherit MIKOSHI_URL / TOKEN / BACKUP_DIR / etc.
# Precedence: env vars set by the user > values from the file.
INGEST_CONF="${MIKOSHI_INGEST_CONF:-${HOME}/.mikoshi-ingest.conf}"
if [[ -f "$INGEST_CONF" ]]; then
    # Snapshot env-provided values so we can restore them after sourcing.
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

    # Env vars trump file values.
    [[ -n "$_saved_MIKOSHI_URL" ]] && export MIKOSHI_URL="$_saved_MIKOSHI_URL"
    [[ -n "$_saved_MIKOSHI_TOKEN" ]] && export MIKOSHI_TOKEN="$_saved_MIKOSHI_TOKEN"
    [[ -n "$_saved_MIKOSHI_BACKUP_DIR" ]] && export MIKOSHI_BACKUP_DIR="$_saved_MIKOSHI_BACKUP_DIR"
    [[ -n "$_saved_MIKOSHI_CLIENT_ID" ]] && export MIKOSHI_CLIENT_ID="$_saved_MIKOSHI_CLIENT_ID"
    [[ -n "$_saved_KEEP_LOCAL_EXPORTS" ]] && export KEEP_LOCAL_EXPORTS="$_saved_KEEP_LOCAL_EXPORTS"
    [[ -n "$_saved_MIKOSHI_FAVORITES_FILE" ]] && export MIKOSHI_FAVORITES_FILE="$_saved_MIKOSHI_FAVORITES_FILE"
fi

favorites_file() {
    echo "${MIKOSHI_FAVORITES_FILE:-${HOME}/.mikoshi-favorites.json}"
}

has_favorites() {
    local f
    f="$(favorites_file)"
    [[ -f "$f" ]] || return 1
    python3 - "$f" <<'PYEOF'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
    sys.exit(0 if data.get("favorites") else 1)
except Exception:
    sys.exit(1)
PYEOF
}

cmd_tui() {
    activate_venv
    exec python3 "$TUI"
}

cmd_sync() {
    activate_venv
    mkdir -p "$LOG_DIR"

    local ts; ts=$(date +%Y%m%d_%H%M%S)
    local cron_log="${LOG_DIR}/cron_${ts}.log"
    local args=()
    local force_all=false
    local force_full=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --all) force_all=true; shift ;;
            --full) force_full=true; shift ;;
            *) args+=("$1"); shift ;;
        esac
    done

    if [[ "$force_full" == true ]]; then
        args+=(--mode full)
    fi

    if [[ "$force_all" != true && "$force_full" != true ]]; then
        if has_favorites; then
            args+=(--favorites)
            echo "[mikoshi] favorites detected → syncing only favorites (incremental)"
        else
            echo "[mikoshi] no favorites file → falling back to incremental over all chats"
        fi
    fi

    echo "[mikoshi] $(date '+%Y-%m-%d %H:%M:%S') starting sync: ${args[*]:-(default)}"
    echo "[mikoshi] full log: $cron_log"
    # Pipe to both terminal and cron log.
    # ${args[@]+"${args[@]}"} is the portable idiom for "expand only if set":
    # bash 3.2 (macOS default) trips on plain "${args[@]}" under `set -u`
    # when the array is empty, e.g. `sync --all` with no other flags.
    "$PIPELINE" ${args[@]+"${args[@]}"} 2>&1 | tee "$cron_log"
    local rc=${PIPESTATUS[0]}
    echo "[mikoshi] $(date '+%Y-%m-%d %H:%M:%S') sync finished (exit $rc)"
    exit "$rc"
}

cmd_status() {
    activate_venv
    python3 - <<'PYEOF'
import sys
sys.path.insert(0, ".")
import tui
tui.action_status()
PYEOF
}

cmd_verify_backup() {
    activate_venv
    exec python3 "${SCRIPT_DIR}/verify_backup.py" "$@"
}

cmd_reset_backup() {
    local force=false
    if [[ "${1:-}" == "--force" ]]; then
        force=true
    fi

    local base="${MIKOSHI_BACKUP_DIR:-${SCRIPT_DIR}/temp}"
    local backup_root="${base}/backup"

    if [[ ! -d "$backup_root" ]]; then
        echo "[mikoshi] no backup found at $backup_root — nothing to reset"
        return 0
    fi

    # Find UDID-named subdirs via simple globbing — avoids bash 3.2 quirks
    # with `< <(find ...)` and arrays under set -u.
    local found=0
    local victim
    for victim in "$backup_root"/*; do
        [[ -d "$victim" ]] || continue
        local name; name=$(basename "$victim")
        if [[ ${#name} -le 20 ]]; then
            continue
        fi
        found=1
        local size
        size=$(du -sh "$victim" 2>/dev/null | cut -f1)
        echo "[mikoshi] candidate for deletion: $victim  ($size)"
    done

    if [[ $found -eq 0 ]]; then
        echo "[mikoshi] no UDID-named directories under $backup_root — nothing to reset"
        return 0
    fi

    echo ""
    echo "This will NOT touch: $base itself, any other files there, or your config."

    if [[ "$force" != true ]]; then
        echo ""
        read -r -p "Type 'yes' to confirm: " ans
        if [[ "$ans" != "yes" ]]; then
            echo "[mikoshi] aborted"
            return 1
        fi
    fi

    for victim in "$backup_root"/*; do
        [[ -d "$victim" ]] || continue
        local name; name=$(basename "$victim")
        if [[ ${#name} -le 20 ]]; then
            continue
        fi
        rm -rf "$victim"
        echo "[mikoshi] ✓ removed $victim"
    done

    # Also wipe any decrypted artifacts from previous runs
    if [[ -d "${base}/extracted" ]]; then
        rm -rf "${base}/extracted"
        echo "[mikoshi] ✓ removed decrypted artifacts"
    fi

    echo "[mikoshi] done. Next sync will start a fresh full backup."
}

# ─── dispatch ──────────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    cmd_tui
fi

case "$1" in
    tui)            shift; cmd_tui "$@" ;;
    sync)           shift; cmd_sync "$@" ;;
    status)         shift; cmd_status "$@" ;;
    reset-backup)   shift; cmd_reset_backup "$@" ;;
    verify-backup)  shift; cmd_verify_backup "$@" ;;
    -h|--help)      usage ;;
    *)              echo "Unknown subcommand: $1"; echo; usage; exit 1 ;;
esac
