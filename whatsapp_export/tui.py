#!/usr/bin/env python3
"""
Interactive menu for the WhatsApp → Mikoshi pipeline.

Built around user intent, not pipeline phases:
  - Sync (the default; shows a plan-before-doing)
  - Inspect (local chats, server cursors, drift)
  - Favorites
  - Setup & verify
  - Tools (advanced)

Persistent status header (refreshed on every menu return) shows the
current state of iPhone / Backup / Decrypt / Server / Drift so the
user never has to guess what's true before clicking. See REDESIGN.md
§5 for the full layout.

Run:  python3 tui.py
"""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

try:
    import questionary
    from questionary import Choice
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("Missing deps. Activate venv first:")
    print("  source .venv/bin/activate && pip install questionary rich")
    sys.exit(1)

import pipeline_state
import tui_cache


SCRIPT_DIR = Path(__file__).parent.resolve()
EXPORTS_DIR = SCRIPT_DIR / "exports"
STATE_FILE = SCRIPT_DIR / ".sync_state.json"
INGEST_CONF = Path(os.environ.get("MIKOSHI_INGEST_CONF", Path.home() / ".mikoshi-ingest.conf"))

console = Console()

IOS_EPOCH = 978307200


# ─── config plumbing (unchanged from pre-redesign) ────────────────────────

INGEST_CONF_KEYS = (
    "MIKOSHI_URL",
    "MIKOSHI_TOKEN",
    "MIKOSHI_BACKUP_DIR",
    "MIKOSHI_CLIENT_ID",
    "KEEP_LOCAL_EXPORTS",
    "MIKOSHI_FAVORITES_FILE",
    "MIKOSHI_PRESERVE_EXTRACTED",
)


def parse_bool(value, *, default):
    if value is None:
        return default
    v = value.strip().lower()
    if v in ("true", "yes", "on", "1"):
        return True
    if v in ("false", "no", "off", "0"):
        return False
    return default


def load_ingest_conf() -> dict:
    cfg = {}
    if INGEST_CONF.exists():
        for line in INGEST_CONF.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for key in INGEST_CONF_KEYS:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
        elif cfg.get(key):
            os.environ[key] = cfg[key]
    return cfg


def set_conf_value(key: str, value: str, *, conf_path: Path | None = None) -> None:
    path = conf_path or INGEST_CONF
    path.parent.mkdir(parents=True, exist_ok=True)
    new_line = f"{key}={value}"
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        k = stripped.split("=", 1)[0].strip()
        if k == key:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)
    os.environ[key] = value


load_ingest_conf()


def get_backup_dir(cfg: dict) -> Path | None:
    val = cfg.get("MIKOSHI_BACKUP_DIR")
    return Path(val) if val else None


PRESERVE_EXTRACTED_DEFAULT = True


# ─── existing helpers (kept verbatim where possible — covered by tests) ───


def find_existing_chatstorage() -> Path | None:
    candidates = [SCRIPT_DIR / "temp" / "extracted" / "ChatStorage.sqlite"]
    cfg = load_ingest_conf()
    if backup_dir := get_backup_dir(cfg):
        candidates.append(backup_dir / "extracted" / "ChatStorage.sqlite")
    for c in candidates:
        if c.exists() and pipeline_state.looks_like_sqlite(c):
            return c
    return None


# Kept for tests that import these symbols directly.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _looks_like_sqlite(path: Path) -> bool:
    return pipeline_state.looks_like_sqlite(path)


def list_chats_from_db(db: Path) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT s.ZCONTACTJID as jid, s.ZPARTNERNAME as name,
               s.ZLASTMESSAGEDATE as last_ts,
               COUNT(m.Z_PK) as msg_count
        FROM ZWACHATSESSION s
        LEFT JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
        WHERE s.ZCONTACTJID IS NOT NULL
        GROUP BY s.Z_PK
        ORDER BY s.ZLASTMESSAGEDATE DESC NULLS LAST
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fmt_ts(ios_ts) -> str:
    if not ios_ts:
        return "—"
    try:
        unix = ios_ts + IOS_EPOCH
        if not 0 <= unix <= 4_102_444_800:
            return "—"
        return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return "—"


def run(cmd: list[str], env_extra: dict | None = None) -> int:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    console.print(f"[dim]$ {' '.join(shlex.quote(c) for c in cmd)}[/]")
    try:
        return subprocess.call(cmd, env=env, cwd=SCRIPT_DIR)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/]")
        return 130


def pause():
    console.print()
    if not sys.stdin.isatty():
        return
    questionary.press_any_key_to_continue("Press any key to return to menu...").ask()


def _dir_size_gb(path: Path, timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return "[dim](du failed)[/]"
        kb = int(result.stdout.split()[0])
        gb = kb / (1024 * 1024)
        if gb >= 1:
            return f"{gb:.1f} GB"
        return f"{kb / 1024:.1f} MB"
    except subprocess.TimeoutExpired:
        return "[dim](still computing — large backup)[/]"
    except Exception as e:
        return f"[dim](error: {e})[/]"


# Thin wrapper kept so existing tests don't break — delegates to the shared
# helper now living in pipeline_state.
def _best_from_phase() -> tuple[int, str]:
    cfg = load_ingest_conf()
    return pipeline_state.best_from_phase(get_backup_dir(cfg))


# ─── multi-source probing (iPhone backup + Mac live) ─────────────────────


SOURCE_DISPLAY = {
    "iphone_backup": "iPhone backup",
    "mac_live": "Mac live",
}


def _favorites_remove_choices(favorites: list[dict]) -> list:
    """Build the questionary.Choice list for "remove favorites".

    Sorted newest-last-message-first using the local ChatStorage when
    available. Favorites referencing chats that no longer exist in the
    local DB sink to the bottom with a dim suffix so the user notices
    them but they don't crowd out the active ones.
    """
    chat_last_ts: dict[str, float] = {}
    db = find_existing_chatstorage()
    if db:
        try:
            for row in list_chats_from_db(db):
                if row.get("jid") and row.get("last_ts") is not None:
                    chat_last_ts[row["jid"]] = float(row["last_ts"])
        except Exception:
            # Reading the DB shouldn't break the picker — degrade to file order
            chat_last_ts = {}

    def _sort_key(fav: dict) -> tuple[int, float]:
        ts = chat_last_ts.get(fav.get("jid"))
        if ts is None:
            return (0, 0.0)  # missing → bottom
        return (1, ts)       # present → ordered DESC by ts

    sorted_favs = sorted(favorites, key=_sort_key, reverse=True)

    choices = []
    for f in sorted_favs:
        label_text = (f.get("name") or f["jid"])[:35]
        if f.get("jid") not in chat_last_ts and chat_last_ts:
            label = f"{label_text}  ({f['jid']})  [no longer in local DB]"
        else:
            label = f"{label_text}  ({f['jid']})"
        choices.append(Choice(label, f["jid"]))
    return choices


def _pick_sources(entries: list[dict]) -> list[str] | None:
    """Ask the user which sources to feed extraction.

    Returns a list of source names ordered ``iphone_backup`` first
    (matches the reconciler's tie-break priority). Returns ``None`` if
    the user cancels.

    Skips the picker entirely when there's no actual choice — single
    available source or none at all (we fall back to iphone_backup so
    the legacy single-source path keeps working).
    """
    available = [e["name"] for e in entries if e["available"]]
    if not available:
        return ["iphone_backup"]  # legacy fallback; phase-1 flow will error if needed
    if len(available) == 1:
        return available
    # Two sources available — present a 3-way choice.
    choice = questionary.select(
        "Which sources should feed this sync?",
        choices=[
            Choice(
                "🪢  Both — reconcile iPhone backup + Mac live (recommended)",
                ["iphone_backup", "mac_live"],
            ),
            Choice("📱  iPhone backup only (full history; media authority)", ["iphone_backup"]),
            Choice("💻  Mac live only (fast; ~3× shorter history; no iPhone needed)", ["mac_live"]),
            Choice("← Cancel", "__cancel__"),
        ],
        instruction="(both will dedup by stanza id; almost zero overlap on Mac-only days)",
    ).ask()
    if choice in (None, "__cancel__"):
        return None
    return choice


def _format_sources_label(selected: list[str], entries: list[dict]) -> str:
    """Human-readable description of the selected sources, with msg counts
    when the snapshot is available."""
    by_name = {e["name"]: e for e in entries}
    bits = []
    for name in selected:
        label = SOURCE_DISPLAY.get(name, name)
        entry = by_name.get(name) or {}
        snap = entry.get("snapshot")
        if snap is not None:
            count = snap.message_count
            if count >= 1_000_000:
                bits.append(f"{label} ({count / 1_000_000:.1f}M msgs)")
            elif count >= 1_000:
                bits.append(f"{label} ({count / 1_000:.0f}k msgs)")
            else:
                bits.append(f"{label} ({count} msgs)")
        else:
            bits.append(label)
    if len(selected) > 1:
        return " + ".join(bits) + " — reconciled"
    return bits[0]


def _probe_sources() -> list[dict]:
    """Cheap availability + snapshot probe of every registered source.

    Returns one dict per registered source with::

        {"name": ..., "available": bool, "snapshot": SourceSnapshot|None, "error": str|None}

    Never raises — the TUI header refresh path depends on this being
    safe to call even when the Mac WhatsApp DB is mid-write or the
    iPhone backup hasn't been produced yet.
    """
    try:
        from sources import IphoneBackupSource, MacLiveSource
    except ImportError:
        return []
    out = []
    for src in (IphoneBackupSource(), MacLiveSource()):
        entry = {"name": src.name, "available": False, "snapshot": None, "error": None}
        try:
            if src.is_available():
                entry["available"] = True
                entry["snapshot"] = src.snapshot()
        except Exception as exc:
            entry["error"] = str(exc)
        out.append(entry)
    return out


# ─── status header (the heart of the new TUI mental model) ────────────────


# Two-tier cache:
#   • In-memory snapshot survives the lifetime of one Python process and is
#     consulted first — refresh costs nothing within a session.
#   • Disk snapshot at SCRIPT_DIR/.tui_cache.json survives across launches
#     so the user gets first paint in milliseconds even on cold start.
#
# When the cached snapshot is stale-but-usable we kick off a daemon thread
# to recompute in the background; the main thread keeps using the cached
# snapshot until the user returns to the menu, at which point the live
# value naturally takes over.
_HEADER_CACHE: dict = {
    "ts": 0.0,
    "ttl": tui_cache.SOFT_TTL,
    "snapshot": None,
}
_REFRESH_LOCK = threading.Lock()
_REFRESH_THREAD: threading.Thread | None = None


def _invalidate_header_cache():
    """Drop both the in-memory and the on-disk cache.

    Called after operations that change state (sync run, schedule change,
    config edit) so the next render reflects reality, not stale numbers.
    """
    _HEADER_CACHE["ts"] = 0.0
    _HEADER_CACHE["snapshot"] = None
    tui_cache.invalidate(SCRIPT_DIR)


def _schedule_info_dict() -> dict | None:
    """Return JSON-safe schedule info or None when no LaunchAgent is installed."""
    try:
        import scheduler
    except Exception:
        return None
    info = scheduler.current_schedule()
    if info is None:
        return None
    now = datetime.now()
    if info.frequency == "hourly":
        target = now.replace(minute=info.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(hours=1)
    else:
        target = now.replace(
            hour=int(info.hour), minute=info.minute, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
    return {
        "enabled": bool(info.enabled),
        "frequency": info.frequency,
        "hour": int(info.hour) if info.hour is not None else None,
        "minute": int(info.minute),
        "next_fire_iso": target.isoformat(timespec="minutes"),
    }


def _last_run_summary_string() -> str | None:
    try:
        import scheduler
        return scheduler.last_run_summary()
    except Exception:
        return None


def _gather_header_snapshot(cfg: dict, *, prev: dict | None = None) -> dict:
    """Compute every field the header displays. ~3s worst case (server probe).

    ``prev`` is the previous snapshot (in-memory or disk) and is used as a
    fallback when ``du -sk`` times out — we'd rather show a stale-but-real
    backup size than the placeholder string.
    """
    snap: dict = {}
    snap["cfg"] = cfg

    # iPhone
    snap["iphone_reachable"] = pipeline_state.device_reachable(timeout=2.0)

    # Backup dir
    bdir = get_backup_dir(cfg)
    snap["backup_dir"] = bdir
    if bdir and bdir.exists():
        udids = pipeline_state.find_udid_dirs(bdir)
        snap["backup_udid_count"] = len(udids)
        if udids:
            size = _dir_size_gb(bdir)
            # On timeout `_dir_size_gb` returns a placeholder; prefer the last
            # real measurement when we have one stashed in the previous snapshot.
            if "still computing" in size and prev and prev.get("backup_size"):
                prev_size = prev["backup_size"]
                if "still computing" not in prev_size and "not set" not in prev_size:
                    size = f"{prev_size} [dim](cached)[/]"
            snap["backup_size"] = size
        else:
            snap["backup_size"] = "[dim]empty[/]"
    else:
        snap["backup_udid_count"] = 0
        snap["backup_size"] = "[dim]not set[/]"

    # Decrypted ChatStorage
    db = find_existing_chatstorage()
    snap["chatstorage"] = db
    if db:
        mtime = db.stat().st_mtime
        snap["chatstorage_mtime"] = datetime.fromtimestamp(mtime, tz=timezone.utc)
    else:
        snap["chatstorage_mtime"] = None

    # Server cursors
    url = cfg.get("MIKOSHI_URL", "").rstrip("/")
    token = cfg.get("MIKOSHI_TOKEN", "")
    snap["server_url"] = url
    if url and token:
        cursors = pipeline_state.fetch_server_cursors(url, token, timeout=3.0)
        snap["server_cursors"] = cursors  # None if endpoint missing / unreachable
    else:
        snap["server_cursors"] = None

    # Cache + drift
    cache = pipeline_state.load_cursor_cache(STATE_FILE)
    snap["cache"] = cache
    snap["last_successful_commit"] = getattr(cache, "last_successful_commit", None)
    drift = pipeline_state.detect_drift(cache, snap["server_cursors"])
    snap["drift"] = drift
    snap["drift_summary"] = {
        status.value: count
        for status, count in pipeline_state.drift_summary(drift).items()
    }

    # Multi-source: iPhone backup + Mac live availability + counts
    snap["sources"] = _probe_sources()

    # Totals for the new "Delta" header row
    server_total = None
    if isinstance(snap["server_cursors"], dict):
        server_total = sum(
            int(getattr(c, "message_count", 0) or 0)
            for c in snap["server_cursors"].values()
        )
    snap["server_total_msgs"] = server_total
    local_max = 0
    for entry in snap["sources"]:
        s = entry.get("snapshot")
        if s is not None:
            local_max = max(local_max, int(getattr(s, "message_count", 0) or 0))
    snap["local_max_msgs"] = local_max if local_max else None

    # Scheduler
    snap["schedule_info"] = _schedule_info_dict()
    snap["last_run_summary"] = _last_run_summary_string()

    return snap


def _rehydrate_from_disk(cached: dict) -> dict:
    """Project a disk-cached snapshot into the in-memory shape the
    renderer expects.

    The renderer reads only JSON-safe fields (see render_header); the
    rich objects (``snap["cache"]``, ``snap["drift"]``) are only used
    by callers that force a live refresh (``action_inspect``), so we
    can leave them out here.
    """
    snap: dict = {}
    snap["cfg"] = {}
    snap["iphone_reachable"] = bool(cached.get("iphone_reachable"))
    bdir = cached.get("backup_dir")
    snap["backup_dir"] = Path(bdir) if bdir else None
    snap["backup_udid_count"] = int(cached.get("backup_udid_count") or 0)
    snap["backup_size"] = cached.get("backup_size") or ""
    chat = cached.get("chatstorage")
    snap["chatstorage"] = Path(chat) if chat else None
    cmt = cached.get("chatstorage_mtime_iso")
    if cmt:
        try:
            snap["chatstorage_mtime"] = datetime.fromisoformat(cmt)
        except ValueError:
            snap["chatstorage_mtime"] = None
    else:
        snap["chatstorage_mtime"] = None
    snap["server_url"] = cached.get("server_url") or ""
    # We don't round-trip the per-JID cursor dict; the renderer only needs
    # the count, so synthesize a sentinel that satisfies len()/None checks.
    cursors_count = cached.get("server_cursors_count")
    snap["server_cursors"] = {"__cached__": cursors_count} if cursors_count is not None else None
    snap["server_cursors_count"] = cursors_count
    snap["server_total_msgs"] = cached.get("server_total_msgs")
    snap["last_successful_commit"] = cached.get("last_successful_commit")
    snap["drift_summary"] = cached.get("drift_summary") or {}

    # Rebuild the "sources" list with lightweight stand-ins for SourceSnapshot
    # (the renderer reads attributes via getattr, so a SimpleNamespace works).
    from types import SimpleNamespace
    sources_out: list[dict] = []
    for entry in cached.get("sources") or []:
        e = {
            "name": entry.get("name"),
            "available": bool(entry.get("available")),
            "error": entry.get("error"),
            "snapshot": None,
        }
        s = entry.get("snapshot")
        if s:
            e["snapshot"] = SimpleNamespace(
                name=s.get("name"),
                db_path=Path(s["db_path"]) if s.get("db_path") else None,
                mtime_iso=s.get("mtime_iso") or "",
                message_count=int(s.get("message_count") or 0),
                media_with_local_path=int(s.get("media_with_local_path") or 0),
            )
        sources_out.append(e)
    snap["sources"] = sources_out

    local_max = 0
    for entry in sources_out:
        s = entry.get("snapshot")
        if s is not None:
            local_max = max(local_max, int(getattr(s, "message_count", 0) or 0))
    snap["local_max_msgs"] = local_max if local_max else None

    snap["schedule_info"] = cached.get("schedule_info")
    snap["last_run_summary"] = cached.get("last_run_summary")
    snap["_from_cache"] = True
    snap["_cache_age_s"] = tui_cache.age_seconds(cached)
    return snap


def _spawn_background_refresh(prev: dict | None) -> None:
    """Refresh the header snapshot off the main thread.

    Idempotent: a single in-flight refresh at a time. We never write to
    the console from here (would garble questionary's terminal state);
    we only update the in-memory and on-disk caches so the next render
    picks up the new value.
    """
    global _REFRESH_THREAD
    with _REFRESH_LOCK:
        if _REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive():
            return

        def _worker():
            try:
                cfg = load_ingest_conf()
                fresh = _gather_header_snapshot(cfg, prev=prev)
                _HEADER_CACHE["snapshot"] = fresh
                _HEADER_CACHE["ts"] = time.time()
                tui_cache.save(SCRIPT_DIR, fresh)
            except Exception:
                # Don't crash the TUI on a background refresh failure —
                # the user will see slightly older data, that's all.
                pass

        t = threading.Thread(target=_worker, name="tui-header-refresh", daemon=True)
        _REFRESH_THREAD = t
        t.start()


def get_header_snapshot(force_refresh: bool = False) -> dict:
    now = time.time()
    # 1) Hot path: fresh in-memory snapshot.
    if (
        not force_refresh
        and _HEADER_CACHE["snapshot"] is not None
        and (now - _HEADER_CACHE["ts"]) < _HEADER_CACHE["ttl"]
    ):
        return _HEADER_CACHE["snapshot"]

    cached = tui_cache.load(SCRIPT_DIR)

    # 2) Disk cache is fresh enough → use it directly, no background work.
    if not force_refresh and tui_cache.is_fresh(cached):
        snap = _rehydrate_from_disk(cached)
        _HEADER_CACHE["snapshot"] = snap
        _HEADER_CACHE["ts"] = now
        return snap

    # 3) Disk cache is stale but usable → return it immediately and kick off
    #    a background refresh that will overwrite it before the user's next loop.
    if not force_refresh and tui_cache.is_usable(cached):
        snap = _rehydrate_from_disk(cached)
        _HEADER_CACHE["snapshot"] = snap
        _HEADER_CACHE["ts"] = now
        _spawn_background_refresh(prev=snap)
        return snap

    # 4) Nothing usable → block on a live gather. First launch ever, or a
    #    forced refresh, or after _invalidate_header_cache().
    cfg = load_ingest_conf()
    snap = _gather_header_snapshot(cfg, prev=cached)
    _HEADER_CACHE["snapshot"] = snap
    _HEADER_CACHE["ts"] = now
    tui_cache.save(SCRIPT_DIR, snap)
    return snap


def render_header(snap: dict) -> None:
    cfg = snap["cfg"]

    iphone_row = (
        "[green]✓ detected[/]"
        if snap["iphone_reachable"]
        else "[yellow]not reachable[/] [dim](OK if you have a cached backup)[/]"
    )

    bdir = snap["backup_dir"]
    if bdir:
        if snap["backup_udid_count"]:
            backup_row = (
                f"[green]{bdir}[/]  "
                f"({snap['backup_size']}, {snap['backup_udid_count']} device)"
            )
        else:
            backup_row = f"[yellow]{bdir}[/]  [dim](no UDID directory yet)[/]"
    else:
        backup_row = "[dim]using SCRIPT_DIR/temp (MIKOSHI_BACKUP_DIR not set)[/]"

    chat = snap["chatstorage"]
    if chat and snap["chatstorage_mtime"]:
        age_min = (datetime.now(tz=timezone.utc) - snap["chatstorage_mtime"]).total_seconds() / 60
        if age_min < 60:
            stale = ""
        elif age_min < 60 * 24:
            stale = f" [dim]({age_min / 60:.0f}h old)[/]"
        else:
            stale = f" [yellow]({age_min / 1440:.0f}d old)[/]"
        decrypt_row = f"[green]✓ ChatStorage at {chat}[/]" + stale
    else:
        decrypt_row = "[yellow]no decrypted DB yet[/]"

    url = snap.get("server_url") or ""
    cursors = snap.get("server_cursors")
    cursors_count = snap.get("server_cursors_count")
    if cursors_count is None and isinstance(cursors, dict) and "__cached__" not in cursors:
        cursors_count = len(cursors)
    if not url:
        server_row = "[red]MIKOSHI_URL not set[/]"
    elif cursors is None:
        server_row = (
            f"[yellow]{url}[/]  [dim]/cursors endpoint unreachable "
            f"(old Mikoshi? Network down? Bad token?)[/]"
        )
    else:
        last_commit = snap.get("last_successful_commit") or "—"
        server_row = (
            f"[green]✓ {url}[/]  "
            f"{cursors_count or 0} chats tracked   "
            f"last commit {(last_commit or '—')[:19]}"
        )

    summary = snap.get("drift_summary") or {}
    n_local_ahead = summary.get(pipeline_state.DriftStatus.LOCAL_AHEAD.value, 0)
    n_in_sync = summary.get(pipeline_state.DriftStatus.IN_SYNC.value, 0)
    n_no_server = summary.get(pipeline_state.DriftStatus.NO_SERVER_RECORD.value, 0)
    n_no_local = summary.get(pipeline_state.DriftStatus.NO_LOCAL_RECORD.value, 0)

    if cursors is None:
        state_row = "[dim]drift unknown (server unreachable)[/]"
    elif n_local_ahead > 0:
        state_row = (
            f"[yellow]⚠ Drift:[/] {n_local_ahead} chats local-ahead "
            "[dim](re-sync will recover; server is authoritative)[/]"
        )
    elif n_in_sync > 0 and n_no_server == 0 and n_no_local == 0:
        state_row = "[green]✓ in-sync[/]"
    else:
        bits = []
        if n_in_sync:
            bits.append(f"{n_in_sync} in-sync")
        if n_no_server:
            bits.append(f"{n_no_server} never-pushed")
        if n_no_local:
            bits.append(f"{n_no_local} server-only")
        state_row = "  ·  ".join(bits) or "[dim](empty)[/]"

    last_sync_row = _last_sync_row(snap)
    schedule_row = _schedule_row(snap)
    delta_row = _delta_row(snap)

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("iPhone", iphone_row)
    table.add_row("Backup", backup_row)
    table.add_row("Decrypt", decrypt_row)
    table.add_row("Server", server_row)
    table.add_row("Sources", _sources_summary_row(snap.get("sources", [])))
    table.add_row("Delta", delta_row)
    table.add_row("Last sync", last_sync_row)
    table.add_row("Schedule", schedule_row)
    table.add_row("State", state_row)

    title = "[bold cyan]Mikoshi WhatsApp[/]"
    if snap.get("_from_cache"):
        age = snap.get("_cache_age_s") or 0.0
        if age >= tui_cache.SOFT_TTL:
            title += f" [dim](cached {_format_age(age)}, refreshing…)[/]"
    console.print(Panel(table, title=title, expand=False))


def _format_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _format_count(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def _last_sync_row(snap: dict) -> str:
    iso = snap.get("last_successful_commit")
    if not iso:
        return "[dim]never (no successful commit recorded yet)[/]"
    try:
        # last_successful_commit is an ISO timestamp written by push_via_api
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - dt).total_seconds()
        when = _format_age(age)
    except ValueError:
        when = iso[:19]
    bits = [f"[green]{when}[/]", f"[dim]{iso[:19]}[/]"]
    last_run = snap.get("last_run_summary")
    if last_run and "exit 0" in last_run:
        bits.append("[green]exit 0[/]")
    elif last_run and "exit" in last_run:
        bits.append("[yellow]non-zero exit[/]")
    return "  ".join(bits)


def _schedule_row(snap: dict) -> str:
    info = snap.get("schedule_info")
    if not info:
        return "[dim]not scheduled[/] [dim](enable in “Schedule automatic sync”)[/]"
    mm = int(info["minute"])
    frequency = info.get("frequency", "daily")
    state = "[green]✓[/]" if info.get("enabled", True) else "[yellow]disabled[/]"
    next_iso = info.get("next_fire_iso")
    next_part = ""
    if next_iso:
        try:
            target = datetime.fromisoformat(next_iso)
            now = datetime.now()
            if frequency == "hourly":
                mins = max(0, int((target - now).total_seconds() // 60))
                next_part = f"  [dim](next in {mins} min)[/]"
            else:
                same_day = target.date() == now.date()
                when = "today" if same_day else "tomorrow"
                next_part = f"  [dim](next {when} {target.strftime('%H:%M')})[/]"
        except ValueError:
            pass
    if frequency == "hourly":
        return f"{state} hourly at [bold]:{mm:02d}[/]{next_part}"
    hh = int(info["hour"])
    return f"{state} daily at [bold]{hh:02d}:{mm:02d}[/]{next_part}"


def _delta_row(snap: dict) -> str:
    server_total = snap.get("server_total_msgs")
    local_max = snap.get("local_max_msgs")
    if server_total is None and local_max is None:
        return "[dim](no source readable, server unreachable)[/]"
    if server_total is None:
        return f"[dim]server unreachable[/]  ·  local max [bold]{_format_count(local_max)}[/]"
    if local_max is None:
        return f"server [bold]{_format_count(server_total)}[/]  ·  [dim]no local source[/]"
    diff = local_max - server_total
    if diff > 0:
        delta = f"[yellow](+{_format_count(diff)} local-only)[/]"
    elif diff < 0:
        delta = f"[dim]({_format_count(-diff)} server-ahead — older history)[/]"
    else:
        delta = "[green](aligned)[/]"
    return (
        f"server [bold]{_format_count(server_total)}[/]  ·  "
        f"local max [bold]{_format_count(local_max)}[/]  {delta}"
    )


def _sources_summary_row(sources: list[dict]) -> str:
    """One-line "Sources" row for the header.

    Both available  → "✓ iPhone bkp 1.0M msgs · ✓ Mac live 304k msgs (fresh 18:42)"
    Only one        → "✓ iPhone bkp 1.0M msgs · ✗ Mac live (not linked)"
    None            → "[dim]none detected[/]"
    """
    if not sources:
        return "[dim]none detected[/]"
    parts = []
    for entry in sources:
        label = "iPhone bkp" if entry["name"] == "iphone_backup" else "Mac live"
        if entry["available"] and entry["snapshot"] is not None:
            snap = entry["snapshot"]
            n = snap.message_count
            if n >= 1_000_000:
                count = f"{n / 1_000_000:.1f}M"
            elif n >= 1_000:
                count = f"{n / 1_000:.0f}k"
            else:
                count = str(n)
            mtime_short = snap.mtime_iso[11:16]  # "HH:MM" out of "YYYY-MM-DDTHH:MM:SS+00:00"
            parts.append(f"[green]✓ {label}[/] {count} msgs [dim](fresh {mtime_short})[/]")
        elif entry["available"]:
            # available_but_snapshot_failed — Mac DB locked at probe time, etc.
            err = entry.get("error") or "snapshot failed"
            parts.append(f"[yellow]? {label}[/] [dim]({err[:40]})[/]")
        else:
            reason = "not linked" if entry["name"] == "mac_live" else "no backup yet"
            parts.append(f"[dim]✗ {label} ({reason})[/]")
    return "  ·  ".join(parts)


# ─── plan screen ─────────────────────────────────────────────────────────


def _scope_jids_for_mode(mode: str, db: Path | None) -> set[str] | None:
    """Resolve a mode (`all`/`favorites`/`one-chat`) into a concrete JID set."""
    if mode == "all":
        return None
    if mode == "favorites":
        import favorites as favs
        jids = favs.jids()
        return set(jids) if jids else set()
    return None  # one-chat handled separately by the caller


def render_plan(
    plan: pipeline_state.Plan,
    *,
    scope_label: str,
    source_label: str,
    sources_label: str | None = None,
) -> None:
    body = Table(show_header=True, header_style="bold cyan", box=None)
    body.add_column("Chat", min_width=20)
    body.add_column("Cutoff", style="dim")

    nonzero = [c for c in plan.chats if c.new_messages > 0]
    multi_source = any(c.per_source for c in nonzero)
    extra_source_names: list[str] = []
    if multi_source:
        seen = set()
        for c in nonzero:
            if not c.per_source:
                continue
            for name in c.per_source:
                if name not in seen:
                    seen.add(name)
                    extra_source_names.append(name)
        # Stable display order: iphone_backup first, then everything else
        # in encounter order. Single column per source with a "+N" label.
        extra_source_names.sort(key=lambda n: (0 if n == "iphone_backup" else 1))
        for name in extra_source_names:
            short = "iPhone +" if name == "iphone_backup" else (
                "Mac +" if name == "mac_live" else f"{name} +"
            )
            body.add_column(short, justify="right")
        body.add_column("Unique≈", justify="right")
    else:
        body.add_column("New msgs", justify="right")
    body.add_column("New att", justify="right")

    for entry in nonzero[:20]:
        cells = [
            (entry.name or entry.jid)[:32],
            (entry.cutoff_ts or "—")[:19],
        ]
        if multi_source:
            for name in extra_source_names:
                ps = (entry.per_source or {}).get(name)
                cells.append(str(ps["new_messages"]) if ps else "—")
            cells.append(str(entry.new_messages))  # merged-unique estimate
        else:
            cells.append(str(entry.new_messages))
        cells.append(str(entry.new_attachments))
        body.add_row(*cells)
    if len(nonzero) > 20:
        # Trailing row keeps the same column count as the header
        tail = ["…", "", f"+{len(nonzero) - 20} more chats"]
        if multi_source:
            tail.extend([""] * len(extra_source_names))
            tail.append("")  # Unique≈
        tail.append("")
        body.add_row(*tail)

    summary = (
        f"[bold]Source:[/]  {source_label}\n"
        f"[bold]Scope:[/]   {scope_label}\n"
        f"[bold]New:[/]     {plan.total_messages} messages across "
        f"{len(nonzero)}/{len(plan.chats)} chats, "
        f"{plan.total_attachments} attachments\n"
    )
    if sources_label:
        summary = f"[bold]Feeds:[/]   {sources_label}\n" + summary
    if not plan.server_endpoint_present:
        summary += (
            "[yellow]⚠[/] Server [dim]/cursors[/] endpoint unreachable — "
            "plan computed from local cache only. Dedup will still protect us, "
            "but the count is an upper bound.\n"
        )

    console.print(Panel(summary, title="[bold]Sync plan[/]", expand=False))
    if nonzero:
        console.print(body)


def compute_plan_if_possible(
    scope_mode: str,
    jid_for_one: str | None,
    selected_sources: list[str] | None = None,
) -> pipeline_state.Plan | None:
    """Return None if we don't have a decrypted DB to plan against yet.

    ``selected_sources`` enables the multi-source plan view: when
    ``mac_live`` is in the list (and available), we hand its DB to
    ``compute_plan`` so each entry gets a ``per_source`` breakdown.
    """
    db = find_existing_chatstorage()
    if not db:
        return None
    cfg = load_ingest_conf()
    cache = pipeline_state.load_cursor_cache(STATE_FILE)
    srv = pipeline_state.fetch_server_cursors(
        cfg.get("MIKOSHI_URL", "").rstrip("/"),
        cfg.get("MIKOSHI_TOKEN", ""),
        timeout=3.0,
    )
    if scope_mode == "one-chat" and jid_for_one:
        scope = {jid_for_one}
    else:
        scope = _scope_jids_for_mode(scope_mode, db)

    extra_dbs = _extra_dbs_for_sources(selected_sources)
    return pipeline_state.compute_plan(
        db, cache, srv, scope_jids=scope, extra_dbs=extra_dbs,
    )


def _extra_dbs_for_sources(selected_sources: list[str] | None) -> dict[str, Path] | None:
    """Map selected source names to their DB paths for the extra-DB
    plan pass. ``iphone_backup`` is the primary DB (already passed
    positionally to compute_plan); only sources OTHER than iphone_backup
    end up here. Returns None when there's no additional source.
    """
    if not selected_sources:
        return None
    try:
        from sources import get_source
    except ImportError:
        return None
    extras: dict[str, Path] = {}
    for name in selected_sources:
        if name == "iphone_backup":
            continue
        try:
            src = get_source(name)
        except KeyError:
            continue
        if not src.is_available():
            continue
        extras[name] = src.db_path()
    return extras or None


# ─── top-level actions (the 5-screen menu) ───────────────────────────────


def _resolve_sources_with_fallback(
    selected: list[str],
    snap: dict,
    phase: int,
    sources_entries: list[dict],
) -> tuple[list[str] | None, int, str | None]:
    """Decide whether to run the sync as picked, degrade to the surviving
    source, or refuse outright.

    Mirrors the cron-path fallback already implemented in
    ``mikoshi-whatsapp.sh:252-260``. The TUI used to shell straight into
    ``run_pipeline.sh`` which doesn't carry that logic, so picking
    "Both" with no iPhone hard-failed at Phase 1.

    Returns ``(resolved_sources, resolved_phase, reason)``:
      * ``reason is None`` → no change, proceed.
      * ``reason == "fatal"`` → ``resolved_sources is None``, caller must
        abort with a red error.
      * any other ``reason`` → caller should show it as a yellow warning
        and confirm before running with the resolved values.
    """
    needs_iphone = (phase < 4) or ("iphone_backup" in selected and phase == 1)
    iphone_reachable = bool(snap.get("iphone_reachable"))
    has_decrypted_db = snap.get("chatstorage") is not None

    by_name = {e.get("name"): e for e in sources_entries}
    mac_entry = by_name.get("mac_live") or {}
    mac_available = bool(mac_entry.get("available")) and mac_entry.get("snapshot") is not None

    iphone_blocked = needs_iphone and not iphone_reachable and not (
        has_decrypted_db and phase >= 4
    )

    if not iphone_blocked:
        return selected, phase, None

    if "mac_live" in selected and mac_available:
        return (
            ["mac_live"],
            4,
            "iPhone unreachable — degrading to Mac-only sync (skipping Phases 1-3).",
        )

    return None, phase, "fatal"


def action_sync():
    """The redesign's centerpiece: plan-then-act sync.

    The user picks scope + source, sees a plan, then confirms.
    """
    snap = get_header_snapshot()

    scope_choice = questionary.select(
        "What scope?",
        choices=[
            Choice("🔂  Favorites (recommended for incremental)", "favorites"),
            Choice("🌍  All chats", "all"),
            Choice("👤  One chat (pick from list)", "one-chat"),
            Choice("← Cancel", "__cancel__"),
        ],
    ).ask()
    if scope_choice in (None, "__cancel__"):
        return

    jid_for_one: str | None = None
    if scope_choice == "one-chat":
        picked = pick_contact()
        if not picked:
            return
        flag, value = picked
        if flag != "--chat-jid":
            console.print("[yellow]Free-form contact match doesn't allow planning; "
                          "running the legacy substring path.[/]")
            jid_for_one = None
            extra_args = ["--mode", "full-contact", flag, value]
        else:
            jid_for_one = value
            extra_args = ["--chat-jid", value]
    else:
        extra_args = []
        if scope_choice == "favorites":
            extra_args.append("--favorites")

    # Sources feed — which data sources will the extractor merge?
    # iPhone backup is the historical / media authority; Mac live is the
    # fresh-but-shallow Catalyst app DB. The user can pick either or both.
    sources_entries = snap.get("sources") or []
    selected_sources = _pick_sources(sources_entries)
    if selected_sources is None:
        return  # user cancelled
    mac_only = selected_sources == ["mac_live"]

    # iPhone-side source pick — only meaningful when iphone_backup feeds
    # extraction. Mac-only sync skips Phases 1-3 entirely.
    default_phase, default_label = _best_from_phase()
    if mac_only:
        phase = 4  # extract runs straight from Mac live DB; no decrypt
    elif default_phase != 1:
        source_choice = questionary.select(
            "How do you want to source the iPhone backup?",
            choices=[
                Choice(f"⚡ {default_label}", default_phase),
                Choice("🔄 Refresh from iPhone (incremental — fetches only new data)", 1),
                Choice("← Cancel", "__cancel__"),
            ],
            instruction="(all are incremental — they differ in how much work to redo)",
        ).ask()
        if source_choice in (None, "__cancel__"):
            return
        phase = source_choice
    else:
        phase = 1

    # Cross-check the picked (sources, phase) against what's actually
    # reachable right now. If the iPhone side is needed but unreachable,
    # try to degrade to Mac-only before bothering the user.
    resolved, phase, reason = _resolve_sources_with_fallback(
        selected_sources, snap, phase, sources_entries,
    )
    if reason == "fatal":
        console.print(
            "[red]No iPhone reachable and no usable Mac live DB.[/]\n"
            "Either plug + unlock + trust the iPhone, or install/open WhatsApp Desktop."
        )
        pause()
        return
    if reason is not None:
        console.print(f"[yellow]⚠ {reason}[/]")
        if not questionary.confirm(
            "Proceed with the degraded sync?", default=True,
        ).ask():
            return
        selected_sources = resolved
        mac_only = selected_sources == ["mac_live"]

    source_label = {
        1: "iPhone (incremental backup → decrypt → extract → push)",
        3: "cached encrypted backup (re-decrypt → extract → push)",
        4: ("cached decrypted DB (extract → push only)" if not mac_only
            else "Mac live DB only (no iPhone needed)"),
    }[phase]

    # Plan (only possible when we already have a decrypted DB, i.e. phase ≥ 4).
    plan: pipeline_state.Plan | None = None
    if phase >= 4 or find_existing_chatstorage():
        plan = compute_plan_if_possible(
            scope_mode="one-chat" if jid_for_one else scope_choice,
            jid_for_one=jid_for_one,
            selected_sources=selected_sources,
        )

    scope_label_human = {
        "favorites": "favorites",
        "all": "all chats",
        "one-chat": jid_for_one or "one chat (manual)",
    }[scope_choice]

    sources_label = _format_sources_label(selected_sources, sources_entries)

    if plan:
        render_plan(
            plan,
            scope_label=scope_label_human,
            source_label=source_label,
            sources_label=sources_label,
        )
    else:
        console.print(Panel(
            f"[bold]Feeds:[/]   {sources_label}\n"
            f"[bold]Source:[/]  {source_label}\n"
            f"[bold]Scope:[/]   {scope_label_human}\n"
            "[dim]Plan not available until ChatStorage has been decrypted.\n"
            "Refresh-from-iPhone runs Phases 1-3 and decrypts on the fly.[/]",
            title="[bold]Sync plan (preview)[/]",
            expand=False,
        ))

    skip_remote = not questionary.confirm("Push to Mikoshi at the end?", default=True).ask()
    proceed = questionary.confirm("Run the sync?", default=True).ask()
    if not proceed:
        return

    cmd = ["bash", str(SCRIPT_DIR / "run_pipeline.sh")]
    cmd.extend(extra_args)
    if phase > 1:
        cmd += ["--from-phase", str(phase)]
    if skip_remote:
        cmd.append("--skip-remote-sync")

    # Only set MIKOSHI_SOURCES when the choice differs from the legacy
    # single-iPhone-source default. Avoids changing the cron-path's
    # behaviour by accident if someone invokes action_sync programmatically.
    env_extra = None
    if selected_sources != ["iphone_backup"]:
        env_extra = {"MIKOSHI_SOURCES": ",".join(selected_sources)}

    # Snapshot server cursors before the run so we can compute the diff
    # post-sync and tell the user how many messages actually landed.
    before_cursors = snap.get("server_cursors")  # may be None if endpoint unreachable

    exit_code = run(cmd, env_extra=env_extra)

    _invalidate_header_cache()
    # Re-fetch server cursors and render the verification panel.
    cfg = load_ingest_conf()
    after_cursors = pipeline_state.fetch_server_cursors(
        cfg.get("MIKOSHI_URL", "").rstrip("/"),
        cfg.get("MIKOSHI_TOKEN", ""),
        timeout=5.0,
    )
    console.print(_verify_sync_result(
        before_cursors=before_cursors,
        after_cursors=after_cursors,
        plan=plan,
        exit_code=exit_code,
        skip_remote=skip_remote,
    ))
    pause()


def _verify_sync_result(
    *,
    before_cursors: dict | None,
    after_cursors: dict | None,
    plan: pipeline_state.Plan | None,
    exit_code: int,
    skip_remote: bool,
) -> Panel:
    """Build the post-sync result Panel.

    Logic:
    - Non-zero exit code → red ✗ with the exit code surfaced.
    - skip_remote → blue "extraction succeeded, push skipped".
    - Server unreachable before/after → yellow ⚠ ("can't verify
      server-side; the pipeline reported success though").
    - Comparing before vs after cursor counts:
        * Sum of per-chat (after.message_count - before.message_count)
          gives the number of new rows the server actually committed.
        * Compare to plan.total_messages (what we expected to push).
        * ≥95% match → green ✓; less → yellow ⚠.
    """
    if exit_code != 0:
        return Panel(
            f"[red]✗ Sync failed[/] (exit code {exit_code}).\n"
            "[dim]Scroll back through the log above for the root cause.[/]",
            title="[bold red]Result[/]", expand=False,
        )

    if skip_remote:
        local = (
            f"{plan.total_messages} messages reconciled locally"
            if plan else "extraction succeeded"
        )
        return Panel(
            f"[cyan]ℹ Sync OK; nothing pushed[/] (--skip-remote-sync).\n"
            f"  {local}.\n"
            "[dim]Re-run without skip to push to Mikoshi.[/]",
            title="[bold]Result[/]", expand=False,
        )

    if after_cursors is None:
        return Panel(
            "[yellow]⚠ Sync completed but server cursor unreachable[/] — "
            "can't independently confirm what landed.\n"
            "[dim]Re-open the TUI in a minute; the header will probe again.[/]",
            title="[bold]Result[/]", expand=False,
        )

    # Cursor-diff path: count new rows on the server.
    new_committed = _count_new_committed(before_cursors or {}, after_cursors)
    expected = plan.total_messages if plan else None

    if expected is None:
        return Panel(
            f"[green]✓ Sync OK[/] — server tracks {len(after_cursors)} chats now.\n"
            f"  {new_committed} new message(s) committed across all chats.",
            title="[bold]Result[/]", expand=False,
        )

    # The plan estimate is an UPPER BOUND (it counts pre-dedup messages
    # from each source). new_committed reflects post-dedup server reality.
    # A 0.95 floor catches the "push truly succeeded" case; below that
    # something was likely dropped.
    if expected == 0:
        verdict = "[green]✓ Sync OK[/] — no new messages to push."
    elif new_committed >= 0.95 * expected:
        verdict = (
            f"[green]✓ Sync confirmed[/] — server committed "
            f"{new_committed} new message(s) (plan estimated {expected})."
        )
    else:
        verdict = (
            f"[yellow]⚠ Mismatch[/] — server committed {new_committed} new "
            f"message(s) but plan estimated {expected}.\n"
            "[dim]Causes: cross-source dedup absorbed duplicates, the push "
            "was partial, or some chats hit append-only cursor protection. "
            "Re-running the sync should be safe.[/]"
        )
    return Panel(verdict, title="[bold]Result[/]", expand=False)


def _count_new_committed(
    before: dict,
    after: dict,
) -> int:
    """Sum of per-chat (after.message_count - before.message_count).

    Both maps key JID → ChatCursor-shaped record. Chats not present in
    `before` count their full after.message_count (they're new chats).
    Cursors with `message_count=None` (very old server response shape)
    contribute 0; we don't have enough info to compute a delta and would
    rather under-report than mislead.
    """
    total = 0
    for jid, cur in after.items():
        cur_count = getattr(cur, "message_count", None)
        if cur_count is None:
            continue
        prev = before.get(jid)
        prev_count = getattr(prev, "message_count", None) if prev else 0
        if prev_count is None:
            prev_count = 0
        delta = int(cur_count) - int(prev_count)
        if delta > 0:
            total += delta
    return total


def action_inspect():
    snap = get_header_snapshot(force_refresh=True)

    drift = snap["drift"]
    cursors = snap["server_cursors"]

    table = Table(title="Per-chat sync state", header_style="bold cyan")
    table.add_column("Chat", min_width=18)
    table.add_column("Status")
    table.add_column("Local cursor", style="dim")
    table.add_column("Server cursor", style="dim")
    table.add_column("Note", style="dim")

    sty = {
        pipeline_state.DriftStatus.IN_SYNC: "[green]✓ in-sync[/]",
        pipeline_state.DriftStatus.LOCAL_AHEAD: "[yellow]⚠ local ahead[/]",
        pipeline_state.DriftStatus.SERVER_AHEAD: "[cyan]↑ server ahead[/]",
        pipeline_state.DriftStatus.NO_SERVER_RECORD: "[dim]no server record[/]",
        pipeline_state.DriftStatus.NO_LOCAL_RECORD: "[dim]no local cache[/]",
    }

    cache = snap["cache"]
    # Decorate with names when we have a DB.
    name_for: dict[str, str] = {}
    db = find_existing_chatstorage()
    if db:
        try:
            for c in list_chats_from_db(db):
                if c.get("jid"):
                    name_for[c["jid"]] = c.get("name") or c["jid"]
        except Exception:
            pass

    for entry in drift[:80]:
        table.add_row(
            (name_for.get(entry.jid) or entry.jid)[:30],
            sty[entry.status],
            (entry.local_ts or "—")[:19],
            (entry.server_ts or "—")[:19],
            entry.note[:60],
        )
    if len(drift) > 80:
        console.print(f"[dim]Showing top 80 of {len(drift)} entries[/]")

    console.print(table)

    if cursors is None:
        console.print(
            "\n[yellow]Server cursor endpoint unreachable[/] — drift detection ran against "
            "local cache only. Check `mikoshi-whatsapp.sh test-auth`."
        )

    sub = questionary.select(
        "More:",
        choices=[
            Choice("Show all local chats (from ChatStorage)", "all"),
            Choice("Open ChatStorage in sqlite3 shell", "sqlite"),
            Choice("← Back", "back"),
        ],
    ).ask()

    if sub == "all":
        action_list_chats()
    elif sub == "sqlite":
        action_sqlite_shell()


def action_list_chats():
    db = find_existing_chatstorage()
    if not db:
        console.print("[yellow]No decrypted ChatStorage found.[/]")
        if questionary.confirm(
            "Decrypt now from existing backup (no iPhone needed)?",
            default=True,
        ).ask():
            run(["bash", str(SCRIPT_DIR / "run_pipeline.sh"),
                 "--from-phase", "3", "--skip-remote-sync"])
        pause()
        return

    chats = list_chats_from_db(db)
    table = Table(title=f"Chats ({len(chats)} total)", header_style="bold cyan")
    table.add_column("Last msg")
    table.add_column("Msgs", justify="right")
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("JID", style="dim")

    for c in chats[:80]:
        kind = "group" if (c["jid"] or "").endswith("@g.us") else "1-on-1"
        table.add_row(
            fmt_ts(c["last_ts"]),
            str(c["msg_count"]),
            kind,
            (c["name"] or "—")[:32],
            c["jid"],
        )
    console.print(table)
    if len(chats) > 80:
        console.print(f"[dim](showing top 80 of {len(chats)})[/]")
    pause()


def pick_contact():
    db = find_existing_chatstorage()
    if db:
        chats = list_chats_from_db(db)
        choices = [
            Choice(
                title=f"{fmt_ts(c['last_ts']):<12} {c['msg_count']:>5} msgs  {(c['name'] or '—')[:30]}",
                value=c["jid"],
            )
            for c in chats[:50] if c["jid"]
        ]
        choices.append(Choice(title="✎ Type name/JID manually", value="__manual__"))
        choices.append(Choice(title="← Cancel", value="__cancel__"))
        pick = questionary.select(
            "Select a contact (or type to filter):",
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
        ).ask()
        if pick is None or pick == "__cancel__":
            return None
        if pick != "__manual__":
            return ("--chat-jid", pick)

    text = questionary.text(
        "Contact name (partial match) or JID:",
        validate=lambda x: bool(x.strip()) or "Required",
    ).ask()
    if not text:
        return None
    if "@" in text:
        return ("--chat-jid", text.strip())
    return ("--contact", text.strip())


# ─── favorites ────────────────────────────────────────────────────────────

import favorites as favs


def _pick_chats_multi(prompt, source_chats, preselect_jids=None):
    preselect_jids = preselect_jids or set()
    choices = []
    for c in source_chats:
        if not c.get("jid"):
            continue
        kind = "group" if c["jid"].endswith("@g.us") else "1-on-1"
        title = f"{fmt_ts(c['last_ts']):<12} {c['msg_count']:>5} msgs  [{kind}]  {(c.get('name') or '—')[:30]}"
        choices.append(Choice(title=title, value=c, checked=(c["jid"] in preselect_jids)))
    if not choices:
        console.print("[red]No chats to choose from.[/]")
        return None
    return questionary.checkbox(
        prompt,
        choices=choices,
        use_search_filter=True,
        use_jk_keys=False,
    ).ask()


def _render_favorites_table():
    data = favs.load()
    items = data.get("favorites", [])
    if not items:
        console.print("[yellow]No favorites yet.[/]")
        return
    cache = pipeline_state.load_cursor_cache(STATE_FILE)
    table = Table(title=f"Favorites ({len(items)})", header_style="bold cyan")
    table.add_column("Name")
    table.add_column("JID", style="dim")
    table.add_column("Last commit", style="dim")
    table.add_column("Added", style="dim")
    for f in items:
        entry = cache.chats.get(f["jid"])
        ts = entry.committed_through_ts if entry else None
        table.add_row(
            (f.get("name") or "—")[:32],
            f["jid"],
            (ts or "—")[:19],
            (f.get("added_at") or "")[:10],
        )
    console.print(table)


def action_favorites():
    while True:
        console.clear()
        render_header(get_header_snapshot())
        console.print()
        _render_favorites_table()
        console.print()

        choice = questionary.select(
            "Manage favorites:",
            choices=[
                Choice("➕  Add chats", "add"),
                Choice("📥  Add all DMs with more than N messages", "add_dms"),
                Choice("➖  Remove chats", "remove"),
                Choice("🗑   Clear all", "clear"),
                Choice("🔂  Sync favorites now", "sync_now"),
                Choice("← Back", "back"),
            ],
        ).ask()

        if choice in (None, "back"):
            return
        if choice == "add":
            db = find_existing_chatstorage()
            if not db:
                console.print("[red]No ChatStorage decrypted yet.[/] Run a sync first.")
                pause()
                continue
            all_chats = list_chats_from_db(db)
            current = {f["jid"] for f in favs.load()["favorites"]}
            picked = _pick_chats_multi(
                "Select chats to add (space to toggle, enter to confirm):",
                all_chats,
                preselect_jids=current,
            )
            if not picked:
                continue
            added = favs.add(
                [{"jid": c["jid"], "name": c.get("name")} for c in picked]
            )
            console.print(f"[green]Added {added} new favorite(s)[/]")
            pause()
        elif choice == "add_dms":
            db = find_existing_chatstorage()
            if not db:
                console.print("[red]No ChatStorage decrypted yet.[/] Run a sync first.")
                pause()
                continue
            threshold_str = questionary.text(
                "Add DMs with at least how many messages?",
                default="100",
                validate=lambda t: t.strip().isdigit() and int(t.strip()) > 0,
            ).ask()
            if not threshold_str:
                continue
            threshold = int(threshold_str.strip())
            all_chats = list_chats_from_db(db)
            matching = favs.filter_dms_with_min_messages(all_chats, threshold)
            if not matching:
                console.print(
                    f"[yellow]No DMs found with ≥ {threshold} messages.[/]"
                )
                pause()
                continue
            existing_jids = {f["jid"] for f in favs.load()["favorites"]}
            new_matching = [c for c in matching if c["jid"] not in existing_jids]
            console.print(
                f"[cyan]Found {len(matching)} DM(s) with ≥ {threshold} messages "
                f"({len(new_matching)} new, {len(matching) - len(new_matching)} "
                f"already favorited).[/]"
            )
            if not new_matching:
                console.print("[dim]Nothing to add — all already in favorites.[/]")
                pause()
                continue
            if not questionary.confirm(
                f"Add {len(new_matching)} new DM(s) to favorites?",
                default=True,
            ).ask():
                continue
            added = favs.add(
                [{"jid": c["jid"], "name": c.get("name")} for c in new_matching]
            )
            console.print(
                f"[green]Added {added} new favorite(s)[/]  "
                f"[dim](union with existing — previous favorites untouched)[/]"
            )
            pause()
        elif choice == "remove":
            data = favs.load()
            if not data["favorites"]:
                console.print("[yellow]No favorites to remove[/]")
                pause()
                continue
            picked = questionary.checkbox(
                "Select favorites to remove:",
                choices=_favorites_remove_choices(data["favorites"]),
                use_search_filter=True,
                use_jk_keys=False,
            ).ask()
            if picked:
                n = favs.remove(picked)
                console.print(f"[green]Removed {n} favorite(s)[/]")
                pause()
        elif choice == "clear":
            if questionary.confirm("Clear ALL favorites?", default=False).ask():
                n = favs.clear()
                console.print(f"[green]Cleared {n} favorite(s)[/]")
                pause()
        elif choice == "sync_now":
            # Short-cut into Sync screen with scope preselected.
            return action_sync()


# ─── setup & verify ──────────────────────────────────────────────────────


def action_setup_verify():
    choice = questionary.select(
        "Setup & verify:",
        choices=[
            Choice("✅  Verify setup (run checks)", "verify_setup"),
            Choice("🔍  Verify backup integrity (1-4 levels)", "verify_backup"),
            Choice("🌐  Test Mikoshi auth (against /cursors)", "test_auth"),
            Choice("✏️   Edit ~/.mikoshi-ingest.conf", "edit_conf"),
            Choice("🔐  Toggle 'keep decrypted between runs'", "toggle"),
            Choice("← Back", "back"),
        ],
    ).ask()
    if choice in (None, "back"):
        return

    if choice == "verify_setup":
        run(["bash", str(SCRIPT_DIR / "verify_setup.sh")])
        pause()
    elif choice == "verify_backup":
        action_verify_backup()
    elif choice == "test_auth":
        run(["bash", str(SCRIPT_DIR / "mikoshi-whatsapp.sh"), "test-auth"])
        pause()
    elif choice == "edit_conf":
        action_edit_config()
    elif choice == "toggle":
        action_toggle_preserve_extracted()


def action_verify_backup():
    level = questionary.select(
        "Which checks?",
        choices=[
            Choice("Level 4 — full (extracts ChatStorage, slowest, definitive)", 4),
            Choice("Level 3 — keybag (passphrase + Manifest.db decrypt, no extract)", 3),
            Choice("Level 2 — Status.plist parses + 'finished'", 2),
            Choice("Level 1 — file presence + magic bytes (instant)", 1),
        ],
    ).ask()
    if not level:
        return
    run([sys.executable, str(SCRIPT_DIR / "verify_backup.py"), "--level", str(level)])
    pause()


def action_edit_config():
    if not INGEST_CONF.exists():
        if not questionary.confirm(f"{INGEST_CONF} doesn't exist. Create with template?", default=True).ask():
            return
        INGEST_CONF.write_text(
            "# Mikoshi WhatsApp pipeline config\n"
            "MIKOSHI_URL=https://your-mikoshi.example.com\n"
            "MIKOSHI_TOKEN=paste-token-here\n"
            "# MIKOSHI_BACKUP_DIR=/Volumes/ExternalSSD/iphone_backup\n"
            "# MIKOSHI_CLIENT_ID=my-mac\n"
            "# KEEP_LOCAL_EXPORTS=5\n"
            "# Keep decrypted ChatStorage + media between runs so --from-phase 4 works.\n"
            "MIKOSHI_PRESERVE_EXTRACTED=true\n"
        )
        INGEST_CONF.chmod(0o600)
    editor = os.environ.get("EDITOR", "nano")
    subprocess.call([editor, str(INGEST_CONF)])
    _invalidate_header_cache()


def action_toggle_preserve_extracted():
    cfg = load_ingest_conf()
    current = parse_bool(cfg.get("MIKOSHI_PRESERVE_EXTRACTED"),
                         default=PRESERVE_EXTRACTED_DEFAULT)

    console.print(Panel(
        f"[bold]Preserve decrypted artifacts across runs[/]\n\n"
        f"Currently: [{'green' if current else 'yellow'}]"
        f"{'ON — extracted/ kept' if current else 'OFF — extracted/ wiped after each run'}[/]",
        title="MIKOSHI_PRESERVE_EXTRACTED",
    ))

    new_val = not current
    label = "Turn OFF" if current else "Turn ON"
    if not questionary.confirm(f"{label}?", default=True).ask():
        return
    set_conf_value("MIKOSHI_PRESERVE_EXTRACTED", "true" if new_val else "false")
    console.print(f"[green]✓ Saved to {INGEST_CONF}[/]")
    _invalidate_header_cache()
    pause()


# ─── help / cheatsheet ────────────────────────────────────────────────────


HELP_TOPICS = [
    ("📖  Main menu actions — what each one does", "actions"),
    ("⚡  Sync command-line flags (mikoshi-whatsapp.sh)", "cli"),
    ("🪢  Two-source model: iPhone backup + Mac live", "sources"),
    ("📍  What is a cursor? (and drift states)", "cursor"),
    ("⏰  LaunchAgent: how the daily/hourly auto-sync works", "schedule"),
    ("📂  Where the user guide lives (in-repo doc)", "guide"),
]


def action_help():
    """One-screen cheatsheet covering every menu action, every wrapper
    flag, the two-source model, cursors, and the LaunchAgent. Picks a
    topic, prints a Rich panel, returns to the topic list. No
    interactivity beyond the picker — read-only documentation."""
    while True:
        choice = questionary.select(
            "Help topics:",
            choices=[Choice(label, key) for label, key in HELP_TOPICS]
                    + [Choice("← Back", "__back__")],
        ).ask()
        if choice in (None, "__back__"):
            return
        console.print(Panel(
            _help_panel(choice),
            title=f"[bold cyan]📚  {dict(HELP_TOPICS)[choice].split('  ', 1)[-1]}[/]",
            expand=False,
        ))
        pause()


def _help_panel(topic: str) -> str:
    """Render a help topic as Rich-formatted prose. Lives in code so it
    stays in lockstep with the TUI's actual behaviour — when an action
    changes, the cheatsheet next to it changes in the same commit."""
    if topic == "actions":
        return (
            "[bold]🔂  Sync[/] — the centerpiece. Pick scope (favorites / all "
            "chats / one chat), pick sources (iPhone backup, Mac live, or "
            "both reconciled), see a plan, then run.\n\n"
            "[bold]📊  Inspect[/] — read-only views: list local chats, view "
            "per-chat drift between local cache and server cursors, see "
            "config status.\n\n"
            "[bold]📌  Manage favorites[/] — pick chats to sync incrementally "
            "(stored in ~/.mikoshi-favorites.json). Plain `sync` uses these "
            "automatically when the file exists.\n\n"
            "[bold]⏰  Schedule automatic sync[/] — install / change / remove "
            "a LaunchAgent that runs `mikoshi-whatsapp.sh sync` either daily "
            "at a user-picked HH:MM or hourly at a user-picked :MM (Mac local).\n\n"
            "[bold]⚙   Setup & verify[/] — run setup checks, validate "
            "Mikoshi auth against /cursor, edit ~/.mikoshi-ingest.conf, "
            "verify backup integrity (1-4 levels of thoroughness).\n\n"
            "[bold]🛠   Tools[/] — advanced: push an existing export to "
            "Mikoshi, refresh local backup only (no push), drop into the "
            "ChatStorage sqlite shell."
        )
    if topic == "cli":
        return (
            "[bold]./mikoshi-whatsapp.sh <subcommand>[/]\n\n"
            "[bold]tui[/]   open the interactive menu (default).\n"
            "[bold]sync[/]  non-interactive incremental sync.\n"
            "   • Default with no flags: auto-detects favorites + sources,\n"
            "     picks the cheapest start phase, exits rc=0 if there's\n"
            "     nothing to sync (cron-friendly).\n"
            "   • [cyan]--all[/]               ignore favorites, sync all chats.\n"
            "   • [cyan]--full[/]              full re-sync from scratch.\n"
            "   • [cyan]--chat-jid <jid>[/]    restrict to one chat.\n"
            "   • [cyan]--since <YYYY-MM-DD>[/] only messages on/after.\n"
            "   • [cyan]--skip-remote-sync[/]  extract but don't push.\n"
            "   • [cyan]--sources <list>[/]    comma-separated source names\n"
            "                          (iphone_backup, mac_live). Default:\n"
            "                          auto-detect.\n\n"
            "[bold]status[/]          print config + backup + cursor state.\n"
            "[bold]test-auth[/]       validate MIKOSHI_TOKEN against /cursor.\n"
            "[bold]reset-backup[/]    wipe partial iPhone backup directory.\n"
            "[bold]verify-backup[/]   integrity check (--level 1-4).\n"
            "[bold]purge-extracted[/] shred decrypted ChatStorage + media.\n"
        )
    if topic == "sources":
        return (
            "Two sources of WhatsApp data this client can read:\n\n"
            "[bold]iphone_backup[/] — the decrypted ChatStorage.sqlite from\n"
            "  an iPhone backup. Slow to refresh (full iPhone backup + decrypt)\n"
            "  but covers the entire message history the iPhone has ever held,\n"
            "  including media bytes on disk.\n\n"
            "[bold]mac_live[/] — the live ChatStorage at\n"
            "  ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/\n"
            "  written by the Mac Catalyst WhatsApp app. Always fresh (Multi-\n"
            "  Device propagation is near-real-time) but ~3× shorter history\n"
            "  and almost no media bytes (mostly thumbnails / cloud-fetch\n"
            "  metadata).\n\n"
            "The reconciler dedups by [bold]ZSTANZAID[/] (stable across\n"
            "devices) → fingerprint fallback (rounded ts + JIDs + text hash)\n"
            "for null-stanza rows. Provenance: when both sources see the\n"
            "same message, iPhone's row wins for attachments (it's the\n"
            "media authority).\n\n"
            "Full spec: docs/design/sources-and-reconciliation.md"
        )
    if topic == "cursor":
        return (
            "A [bold]cursor[/] is one chat's commit watermark — \"the latest\n"
            "message we've already pushed for this chat.\" Two stores:\n\n"
            "  • [bold]Server-side[/] (Mikoshi's [cyan]ingestion_cursor[/] table) — the\n"
            "    canonical, authoritative value. The TUI fetches it via\n"
            "    GET /api/ingest/v1/cursor.\n"
            "  • [bold]Local cache[/] ([cyan].sync_state.json[/]) — a mirror of the\n"
            "    server's view. Read-only after a successful commit; never\n"
            "    advances ahead of the server.\n\n"
            "[bold]Drift states[/] in the header / Inspect screen:\n"
            "  • [green]IN_SYNC[/] — local cache matches server.\n"
            "  • [cyan]SERVER_AHEAD[/] — another client pushed; refresh.\n"
            "  • [yellow]NO_LOCAL_RECORD[/] — server tracks this chat but we\n"
            "    don't yet (fresh install / wiped cache).\n"
            "  • [dim]NO_SERVER_RECORD[/] — local has data, server hasn't seen\n"
            "    it yet (next push will create the cursor).\n"
            "  • [red]LOCAL_AHEAD[/] — should not happen post-redesign;\n"
            "    indicates a bug or a manual edit of .sync_state.json."
        )
    if topic == "schedule":
        return (
            "The Schedule action manages a single LaunchAgent at\n"
            "[cyan]~/Library/LaunchAgents/com.mikoshi.sync.plist[/].\n\n"
            "When enabled, launchd runs [bold]./mikoshi-whatsapp.sh sync[/]\n"
            "on one of two cadences (Mac local time):\n"
            "  • [bold]daily[/]  at a user-picked HH:MM\n"
            "  • [bold]hourly[/] at a user-picked :MM (1× per hour)\n"
            "Sync uses auto-detected sources + favorites, so the agent\n"
            "does the right thing without any further configuration.\n\n"
            "Why launchd vs cron: launchd survives sleep, recovers missed\n"
            "runs after wake, and integrates with macOS's power-management.\n"
            "Cron still works if the user prefers — see README's manual\n"
            "snippet — but the TUI assistant only manages the LaunchAgent.\n\n"
            "Logs land under [cyan]logs/launchagent.out.log[/] and\n"
            "[cyan]logs/launchagent.err.log[/]; per-run cron-style logs\n"
            "appear at [cyan]logs/cron_<timestamp>.log[/]."
        )
    if topic == "guide":
        return (
            "Deeper documentation lives in-repo at:\n\n"
            "  • [cyan]docs/USER_GUIDE.md[/]  step-by-step setup + workflow\n"
            "  • [cyan]docs/design/sources-and-reconciliation.md[/]  source model\n"
            "  • [cyan]docs/design/accounts.md[/]  server-side ingestion + cursors\n"
            "  • [cyan]README.md[/]  project overview + quick start\n"
            "  • [cyan]REDESIGN.md[/]  why the TUI looks the way it does\n"
            "\n"
            "Run from the project root:\n"
            "  [bold]./mikoshi-whatsapp.sh --help[/]  for the wrapper's flag\n"
            "  cheatsheet (same content as the 'CLI flags' topic in this menu)."
        )
    return "Unknown topic."


# ─── scheduled automatic sync (LaunchAgent) ──────────────────────────────


def action_schedule():
    """Activate, change, or deactivate the daily LaunchAgent sync.

    Shows current state, last-run summary, and the four typical actions.
    The actual install/uninstall logic lives in ``scheduler.py`` so it
    can be unit-tested without going near the TUI.
    """
    import re

    import scheduler

    while True:
        info = scheduler.current_schedule()
        if info is None:
            status = "[dim]Not scheduled — automatic sync is disabled.[/]"
            current_label = None
            current_frequency = None
        else:
            current_frequency = info.frequency
            if info.frequency == "hourly":
                current_label = f":{info.minute:02d}"
                cadence_desc = f"hourly at [bold]{current_label}[/]"
            else:
                current_label = f"{info.hour:02d}:{info.minute:02d}"
                cadence_desc = f"daily at [bold]{current_label}[/]"
            state = "[green]enabled[/]" if info.enabled else "[yellow]disabled[/]"
            status = (
                f"{state}, {cadence_desc} (Mac local time)\n"
                f"[dim]Plist: {info.plist_path}[/]"
            )

        last = scheduler.last_run_summary()
        body = status
        if last:
            body += f"\n[dim]Last run log: {last}[/]"

        console.print(Panel(
            body,
            title="[bold]⏰  Scheduled automatic sync[/]",
            expand=False,
        ))

        choices = []
        if info is None:
            choices.append(Choice("🟢  Enable daily — pick HH:MM", "enable_daily"))
            choices.append(Choice("🕒  Enable hourly — pick :MM (1× per hour)", "enable_hourly"))
        else:
            if current_frequency == "hourly":
                choices.append(Choice(f"🔁  Change hourly minute (currently {current_label})", "enable_hourly"))
                choices.append(Choice("📅  Switch to daily at HH:MM", "enable_daily"))
            else:
                choices.append(Choice(f"🔁  Change daily time (currently {current_label})", "enable_daily"))
                choices.append(Choice("🕒  Switch to hourly (1× per hour at :MM)", "enable_hourly"))
            choices.append(Choice("🔴  Disable — remove the LaunchAgent", "disable"))
        choices.append(Choice("← Back", "__back__"))

        action = questionary.select("What now?", choices=choices).ask()
        if action in (None, "__back__"):
            return

        if action == "enable_daily":
            default_time = (
                current_label
                if current_label and current_frequency == "daily"
                else "06:00"
            )
            time_str = questionary.text(
                "Daily run time (HH:MM, Mac local):",
                default=default_time,
                validate=lambda t: bool(re.fullmatch(r"\d{2}:\d{2}", t.strip())) and
                                   _valid_hhmm(t.strip()),
            ).ask()
            if not time_str:
                continue
            hour, minute = (int(x) for x in time_str.strip().split(":"))
            try:
                path = scheduler.install_schedule(hour, minute, frequency="daily")
                console.print(
                    f"[green]✓ LaunchAgent installed:[/] daily at {hour:02d}:{minute:02d}.\n"
                    f"[dim]{path}[/]"
                )
            except (RuntimeError, ValueError, FileNotFoundError) as e:
                console.print(f"[red]Failed:[/] {e}")
            pause()

        elif action == "enable_hourly":
            default_minute = (
                f"{info.minute:02d}"
                if info and current_frequency == "hourly"
                else "15"
            )
            minute_str = questionary.text(
                "Run every hour at minute (00-59):",
                default=default_minute,
                validate=lambda t: t.strip().isdigit() and 0 <= int(t.strip()) <= 59,
            ).ask()
            if not minute_str:
                continue
            minute = int(minute_str.strip())
            try:
                path = scheduler.install_schedule(None, minute, frequency="hourly")
                console.print(
                    f"[green]✓ LaunchAgent installed:[/] hourly at :{minute:02d}.\n"
                    f"[dim]{path}[/]"
                )
            except (RuntimeError, ValueError, FileNotFoundError) as e:
                console.print(f"[red]Failed:[/] {e}")
            pause()

        elif action == "disable":
            if not questionary.confirm(
                "Remove the daily LaunchAgent? (Manual syncs still work.)",
                default=True,
            ).ask():
                continue
            removed = scheduler.disable_schedule()
            if removed:
                console.print("[green]✓ Disabled.[/] LaunchAgent removed.")
            else:
                console.print("[yellow]Nothing to remove (already disabled).[/]")
            pause()


def _valid_hhmm(s: str) -> bool:
    try:
        hh, mm = s.split(":")
        return 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
    except (ValueError, AttributeError):
        return False


# ─── tools (advanced) ─────────────────────────────────────────────────────


def action_tools():
    choice = questionary.select(
        "Advanced tools:",
        choices=[
            Choice("📤  Push existing export to Mikoshi", "push_existing"),
            Choice("📥  Refresh local backup only (no push)", "refresh_local"),
            Choice("🐚  Open sqlite3 shell on ChatStorage", "sqlite"),
            Choice("🧹  Purge decrypted artifacts (shred)", "purge"),
            Choice("🧪  Run tests", "tests"),
            Choice("🔄  Refresh header now", "refresh"),
            Choice("← Back", "back"),
        ],
    ).ask()
    if choice in (None, "back"):
        return

    if choice == "push_existing":
        action_push_existing()
    elif choice == "refresh_local":
        action_refresh_local()
    elif choice == "sqlite":
        action_sqlite_shell()
    elif choice == "purge":
        run(["bash", str(SCRIPT_DIR / "mikoshi-whatsapp.sh"), "purge-extracted"])
        _invalidate_header_cache()
        pause()
    elif choice == "tests":
        run([sys.executable, "-m", "pytest", "-v"])
        pause()
    elif choice == "refresh":
        _invalidate_header_cache()


def action_refresh_local():
    """Phases 1-4: backup from iPhone → decrypt → extract, WITHOUT push."""
    snap = get_header_snapshot()
    cfg = snap["cfg"]

    console.print(Panel(
        "Will backup from iPhone, decrypt, and extract — "
        "but skip push to Mikoshi server.",
        title="[bold]📥 Refresh local backup only[/]",
        expand=False,
    ))

    # Determine source, same logic as action_sync.
    iphone_reachable = snap["iphone_reachable"]
    backup_dir = get_backup_dir(cfg)
    best_phase, best_label = pipeline_state.best_from_phase(backup_dir)

    if iphone_reachable:
        phase = 1
        source_desc = "from iPhone (phase 1 — incremental backup → decrypt → extract)"
    elif best_phase in (3, 4):
        # Cap at phase 3: re-decrypt; phase 4 (extract-only) is fine too.
        phase = best_phase
        source_desc = f"{best_label} (iPhone not reachable)"
    else:
        # best_phase == 1 but iPhone not reachable and no backup exists.
        console.print(
            "[red]No iPhone reachable and no cached backup found.[/]\n"
            "Plug the iPhone (unlock + trust) and try again."
        )
        pause()
        return

    console.print(f"[cyan]Source:[/] {source_desc}")

    # Scope: favorites if file has entries, else all chats.
    fav_jids = favs.jids()
    if fav_jids:
        scope_flags = ["--favorites"]
        scope_desc = f"favorites ({len(fav_jids)} chats)"
    else:
        scope_flags = []
        scope_desc = "all chats"
    console.print(f"[cyan]Scope:[/]  {scope_desc}")
    console.print()

    proceed = questionary.confirm(
        "Run local backup/decrypt/extract (no push)?", default=True
    ).ask()
    if not proceed:
        return

    cmd = ["bash", str(SCRIPT_DIR / "run_pipeline.sh")]
    cmd.extend(scope_flags)
    if phase > 1:
        cmd += ["--from-phase", str(phase)]
    cmd.append("--skip-remote-sync")
    run(cmd)

    _invalidate_header_cache()

    # Show where the extracted data landed.
    extracted_dir = (backup_dir / "extracted") if backup_dir else (SCRIPT_DIR / "temp" / "extracted")
    console.print(f"\n[green]Done.[/] Extracted data at: [cyan]{extracted_dir}[/]")
    pause()


def action_push_existing():
    exports = sorted(EXPORTS_DIR.glob("whatsapp_export_*.json"), reverse=True)
    if not exports:
        console.print("[red]No local exports found in exports/[/]")
        pause()
        return

    cfg = load_ingest_conf()
    if not cfg.get("MIKOSHI_URL") or not cfg.get("MIKOSHI_TOKEN"):
        console.print("[red]MIKOSHI_URL and MIKOSHI_TOKEN are required.[/]")
        console.print(f"Edit {INGEST_CONF}")
        pause()
        return

    choices = []
    for e in exports[:20]:
        try:
            meta = json.loads(e.read_text())
            choices.append(Choice(
                title=f"{e.name}  [{meta.get('mode')}, {meta['stats']['total_messages']} msgs]",
                value=str(e),
            ))
        except Exception:
            choices.append(Choice(title=e.name, value=str(e)))

    pick = questionary.select("Pick an export to push:", choices=choices).ask()
    if not pick:
        return
    run([
        sys.executable, str(SCRIPT_DIR / "push_via_api.py"),
        "--manifest", pick,
        "--attachments-dir", str(EXPORTS_DIR / "attachments"),
        "--state-file", str(STATE_FILE),
    ])
    _invalidate_header_cache()
    pause()


def action_sqlite_shell():
    db = find_existing_chatstorage()
    if not db:
        console.print("[yellow]No decrypted ChatStorage. Decrypting now...[/]")
        run(["bash", str(SCRIPT_DIR / "run_pipeline.sh"),
             "--from-phase", "3", "--skip-remote-sync"])
        return
    console.print(f"[cyan]Opening sqlite3 against {db}[/]")
    console.print("[dim]Type .quit to return[/]")
    os.execvp("sqlite3", ["sqlite3", str(db)])


# ─── action_status (kept for `mikoshi-whatsapp.sh status`) ───────────────


def action_status():
    """Headless-friendly status dump used by `mikoshi-whatsapp.sh status`."""
    snap = get_header_snapshot(force_refresh=True)
    render_header(snap)
    # Plus a few extra fields not in the header.
    cfg = snap["cfg"]
    table = Table(title="More details", show_header=False, box=None)
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("Config file", str(INGEST_CONF) + (" ✓" if INGEST_CONF.exists() else " ✗"))
    table.add_row("MIKOSHI_TOKEN", "[green]set[/]" if cfg.get("MIKOSHI_TOKEN") else "[red]not set[/]")
    exports = sorted(EXPORTS_DIR.glob("whatsapp_export_*.json"))
    table.add_row("Local exports", f"{len(exports)} files" if exports else "[dim]none[/]")
    try:
        fav_count = len(favs.load().get("favorites", []))
        table.add_row("Favorites", f"{fav_count} chat(s)" if fav_count else "[dim]none[/]")
    except Exception:
        pass
    console.print(table)
    pause()


# ─── main loop ────────────────────────────────────────────────────────────

# Intent-based top-level actions (REDESIGN.md §5.1).
# The label text includes the cross-reference keyword "Sync" / "Push" / etc.
# in sub-screens so users still find them via the menu's search filter.
ACTIONS = [
    ("🔂  Sync (recommended)",           "sync"),
    ("📊  Inspect (List chats, drift, status)", "inspect"),
    ("📌  Manage favorites",             "favorites"),
    ("⏰  Schedule automatic sync (LaunchAgent, daily or hourly)", "schedule"),
    ("⚙   Setup & verify (Verify setup, auth, config)", "setup"),
    ("🛠   Tools (Push, sqlite, advanced)", "tools"),
    ("📚  Help — cheatsheet & docs",      "help"),
]

_ACTION_DISPATCH = {
    "sync": action_sync,
    "inspect": action_inspect,
    "favorites": action_favorites,
    "schedule": action_schedule,
    "setup": action_setup_verify,
    "tools": action_tools,
    "help": lambda: action_help(),
}


_EXIT_SENTINEL = "__exit__"


def main():
    while True:
        console.clear()
        snap = get_header_snapshot()
        render_header(snap)
        console.print()

        choice = questionary.select(
            "What do you want to do?",
            choices=[Choice(title=label, value=key) for label, key in ACTIONS]
                    + [Choice(title="🚪  Exit", value=_EXIT_SENTINEL)],
            use_shortcuts=True,
        ).ask()

        if choice in (None, _EXIT_SENTINEL):
            break
        fn = _ACTION_DISPATCH.get(choice)
        if fn is None:
            console.print(f"[yellow]Unexpected selection: {choice!r}[/]")
            continue
        try:
            fn()
        except KeyboardInterrupt:
            console.print("\n[yellow]Action cancelled[/]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[cyan]Bye![/]")
