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
#   ./mikoshi-whatsapp.sh purge-extracted [--force]  # opt-in shred of decrypted artifacts
#   ./mikoshi-whatsapp.sh test-auth      # verify MIKOSHI_URL/MIKOSHI_TOKEN against the server
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
  sync [OPTS]  Non-interactive sync. Suitable for cron / LaunchAgent.
                  --all                Sync all chats (ignore favorites).
                  --full               Full re-sync from scratch.
                  --chat-jid <jid>     Restrict to one chat (selective decrypt).
                  --since <date>       Only messages since YYYY-MM-DD.
                  --skip-remote-sync   Run extraction but don't push.
                  --sources <list>     Comma-separated source names — override
                                       the default auto-detect. Currently
                                       supported: iphone_backup, mac_live.
                  Anything else gets forwarded to run_pipeline.sh.

                  Default behaviour (no flags):
                    1. Auto-pick sources: both iPhone backup and Mac live
                       if both are available; whichever single source is
                       present otherwise; clean rc=0 exit if neither is.
                       The reconciler dedups across sources by stanza id.
                    2. Auto-detect favorites: if ~/.mikoshi-favorites.json
                       exists, sync only those chats (incremental). Else
                       fall back to all chats.
                    3. Auto-pick the cheapest start phase based on what's
                       on disk — cached backup → skip backup phase; cached
                       decrypted DB → skip decrypt phase; Mac-only sync
                       skips iPhone phases entirely.

  status       Print pipeline status (config, backup, sync state, drift).
  reset-backup [--force]
               Delete the partial/corrupt iPhone backup so the next sync
               can start fresh. Only touches MIKOSHI_BACKUP_DIR/backup/<UDID>/.
  verify-backup [--level 1-4]
               Run integrity checks against the existing encrypted backup
               without touching it.
  purge-extracted [--force]
               Shred decrypted artifacts (ChatStorage.sqlite, *.plist,
               Manifest.db). The encrypted backup stays intact. Replaces
               the old "Phase 5 secure_cleanup" which used to run on every
               successful sync and forced re-decrypting on the next run.
  test-auth    Hit the Mikoshi cursors endpoint with the configured token
               and return a friendly diagnosis. Doesn't push anything.
  -h, --help   This message.

Examples:
  $(basename "$0")                                # interactive TUI
  $(basename "$0") sync                           # favorites if any, else all
  $(basename "$0") sync --all                     # force all-chats incremental
  $(basename "$0") sync --skip-remote-sync        # dry run, no push
  $(basename "$0") test-auth                      # validate MIKOSHI_TOKEN

  # Single chat, since a date — selectively decrypts only that chat's media:
  $(basename "$0") sync --chat-jid '34xxxxxxxxx@s.whatsapp.net' --since 2026-01-01

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

# Load ~/.mikoshi-ingest.conf so child processes inherit the env vars.
INGEST_CONF="${MIKOSHI_INGEST_CONF:-${HOME}/.mikoshi-ingest.conf}"
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

# Cron / interactive parity: share `_best_from_phase` with the TUI by
# shelling out to pipeline_state. Stdout: "<phase>\t<label>". Stderr is
# human-readable. Pre-redesign the cron path always started from Phase 1
# and failed when the iPhone wasn't around — closes pain point #9.
#
# PYTHONPATH gets SCRIPT_DIR prepended so the import works no matter
# what the caller's CWD is (cron typically calls us from $HOME).
best_phase() {
    activate_venv
    # `cd` into SCRIPT_DIR so `python3 -m pipeline_state` resolves the
    # module from the install dir, not from the caller's CWD (cron typically
    # runs us from $HOME, and the test harness from the project root —
    # both cases the wrapper has to handle). The cd is local to the
    # subshell created by command substitution in the caller, so it
    # doesn't leak.
    (cd "$SCRIPT_DIR" && python3 -m pipeline_state best-phase 2>/dev/null) \
        || echo "1	Refresh from iPhone"
}

# Wrapper around the require-iphone probe — same `cd` trick.
require_iphone_ok() {
    ( cd "$SCRIPT_DIR" && python3 -m pipeline_state best-phase --require-iphone >/dev/null 2>&1 )
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
            --sources)
                # Pass the comma-separated source list through to
                # run_pipeline.sh as MIKOSHI_SOURCES. Wrapper-side
                # validation is deliberately loose — the Python side
                # rejects unknown names with a clear error.
                [[ -z "${2:-}" ]] && { echo "[mikoshi] --sources requires a value" >&2; exit 1; }
                export MIKOSHI_SOURCES="$2"
                shift 2
                ;;
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

    # Auto-detect sources when the user didn't pass --sources explicitly.
    # detect-sources returns one name per line, in reconciler priority order.
    # Empty output → neither iPhone backup nor Mac live DB is available;
    # exit 0 cleanly (cron: "nothing to do" beats "failed").
    if [[ -z "${MIKOSHI_SOURCES:-}" ]]; then
        local detected
        detected=$(cd "$SCRIPT_DIR" && python3 -m pipeline_state detect-sources | paste -sd, -)
        if [[ -z "$detected" ]]; then
            echo "[mikoshi] no iPhone backup AND no Mac live DB available → nothing to sync (rc=0)"
            exit 0
        fi
        export MIKOSHI_SOURCES="$detected"
        echo "[mikoshi] auto-detected sources: $MIKOSHI_SOURCES"
    fi

    # Smart phase selection — same logic the TUI uses (REDESIGN.md §6.2).
    # If the user didn't pass --from-phase, pick the cheapest based on
    # on-disk state + iPhone reachability.
    local has_from_phase=false
    for a in "${args[@]+"${args[@]}"}"; do
        if [[ "$a" == "--from-phase" ]]; then
            has_from_phase=true
            break
        fi
    done

    if [[ "$has_from_phase" != true ]]; then
        # When iphone_backup is NOT in the auto-picked sources (i.e. we're
        # syncing Mac-live-only), there's no decrypted iPhone DB to feed
        # extract from — but extract reads its DB directly from the Mac
        # source object, so we just skip phases 1-3 outright.
        if [[ ",${MIKOSHI_SOURCES}," != *",iphone_backup,"* ]]; then
            echo "[mikoshi] Mac-only sync (sources=$MIKOSHI_SOURCES) → starting from phase 4"
            args+=(--from-phase 4)
        else
            local best_out
            best_out=$(best_phase)
            local phase="${best_out%%	*}"
            local label="${best_out#*	}"
            if [[ "$phase" == "1" ]]; then
                # iPhone-side phase 1 needs the iPhone reachable. If it's
                # not AND we have no cached backup, fall back to whatever
                # other source is available; if that's also empty, exit
                # cleanly with rc=0 (cron: "nothing to do" beats failed).
                if ! require_iphone_ok; then
                    if [[ ",${MIKOSHI_SOURCES}," == *",mac_live,"* ]]; then
                        export MIKOSHI_SOURCES="mac_live"
                        echo "[mikoshi] no iPhone reachable → falling back to Mac-only sync"
                        args+=(--from-phase 4)
                    else
                        echo "[mikoshi] no iPhone reachable and no cached backup → nothing to do (rc=0)"
                        exit 0
                    fi
                fi
            else
                echo "[mikoshi] smart-phase: starting from phase $phase ($label)"
                args+=(--from-phase "$phase")
            fi
        fi
    fi

    # Pre-flight: confirm the server cursor is reachable. The redesign
    # made server-side cursors authoritative — degrading to a stale local
    # cache is exactly what caused the original drift bug. Exit 3 if the
    # server isn't answering /cursor and the user hasn't opted into the
    # MIKOSHI_TRUST_LOCAL_CURSOR=1 escape hatch.
    if ! (cd "$SCRIPT_DIR" && python3 -m pipeline_state check-server-cursor); then
        echo "[mikoshi] aborting sync: server cursor unreachable." >&2
        echo "[mikoshi] (set MIKOSHI_TRUST_LOCAL_CURSOR=1 to proceed with stale local cursors)" >&2
        exit 3
    fi

    echo "[mikoshi] $(date '+%Y-%m-%d %H:%M:%S') starting sync: ${args[*]:-(default)}"
    echo "[mikoshi] full log: $cron_log"
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

    if [[ -d "${base}/extracted" ]]; then
        rm -rf "${base}/extracted"
        echo "[mikoshi] ✓ removed decrypted artifacts"
    fi

    echo "[mikoshi] done. Next sync will start a fresh full backup."
}

# Opt-in secure cleanup. Replaces the pre-redesign "Phase 5" that ran
# unconditionally on every successful sync.
cmd_purge_extracted() {
    local force=false
    if [[ "${1:-}" == "--force" ]]; then
        force=true
    fi

    local base="${MIKOSHI_BACKUP_DIR:-${SCRIPT_DIR}/temp}"
    local extracted="${base}/extracted"

    if [[ ! -d "$extracted" ]]; then
        echo "[mikoshi] no decrypted artifacts at $extracted — nothing to purge"
        return 0
    fi

    local size
    size=$(du -sh "$extracted" 2>/dev/null | cut -f1)
    echo "[mikoshi] will shred: $extracted ($size)"
    echo "  Files to shred: ChatStorage.sqlite, *.plist, Manifest.db (encrypted backup left intact)"

    if [[ "$force" != true ]]; then
        echo ""
        read -r -p "Type 'yes' to confirm: " ans
        if [[ "$ans" != "yes" ]]; then
            echo "[mikoshi] aborted"
            return 1
        fi
    fi

    find "$extracted" -type f \( \
        -name "ChatStorage.sqlite" -o \
        -name "*.plist" -o \
        -name "Manifest.db" \
    \) -exec shred -vfz -n 7 {} \; 2>/dev/null || true

    # Sweep decrypted media (large) and the extracted/ directory itself.
    rm -rf "$extracted"
    echo "[mikoshi] ✓ decrypted artifacts removed"
    echo "[mikoshi]   next sync needs --from-phase 3 (re-decrypt) at minimum"
}

cmd_test_auth() {
    activate_venv
    python3 - <<'PYEOF'
import os
import sys
sys.path.insert(0, ".")
import push_via_api

cfg = push_via_api.load_config()
url = cfg.get("MIKOSHI_URL", "").rstrip("/")
token = cfg.get("MIKOSHI_TOKEN", "")
ok, msg = push_via_api.test_auth(url, token)
print(msg)
sys.exit(0 if ok else 1)
PYEOF
}

# ─── dispatch ──────────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then
    cmd_tui
fi

case "$1" in
    tui)              shift; cmd_tui "$@" ;;
    sync)             shift; cmd_sync "$@" ;;
    status)           shift; cmd_status "$@" ;;
    reset-backup)     shift; cmd_reset_backup "$@" ;;
    verify-backup)    shift; cmd_verify_backup "$@" ;;
    purge-extracted)  shift; cmd_purge_extracted "$@" ;;
    test-auth)        shift; cmd_test_auth "$@" ;;
    -h|--help)        usage ;;
    *)                echo "Unknown subcommand: $1"; echo; usage; exit 1 ;;
esac
