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
import time
from datetime import datetime, timezone
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


# Cache server probes for ~30 seconds so the menu doesn't feel sluggish.
# Setting this to 0 forces a refresh on every render — used by the "Refresh"
# action and after operations that change state.
_HEADER_CACHE: dict = {"ts": 0.0, "ttl": 30.0, "snapshot": None}


def _invalidate_header_cache():
    _HEADER_CACHE["ts"] = 0.0


def _gather_header_snapshot(cfg: dict) -> dict:
    """Compute every field the header displays. ~3s worst case (server probe)."""
    snap = {}
    snap["cfg"] = cfg

    # iPhone
    snap["iphone_reachable"] = pipeline_state.device_reachable(timeout=2.0)

    # Backup dir
    bdir = get_backup_dir(cfg)
    snap["backup_dir"] = bdir
    if bdir and bdir.exists():
        udids = pipeline_state.find_udid_dirs(bdir)
        snap["backup_udid_count"] = len(udids)
        snap["backup_size"] = _dir_size_gb(bdir) if udids else "[dim]empty[/]"
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
    snap["drift"] = pipeline_state.detect_drift(cache, snap["server_cursors"])

    # Multi-source: iPhone backup + Mac live availability + counts
    snap["sources"] = _probe_sources()
    return snap


def get_header_snapshot(force_refresh: bool = False) -> dict:
    now = time.time()
    if (
        not force_refresh
        and _HEADER_CACHE["snapshot"] is not None
        and (now - _HEADER_CACHE["ts"]) < _HEADER_CACHE["ttl"]
    ):
        return _HEADER_CACHE["snapshot"]
    cfg = load_ingest_conf()
    snap = _gather_header_snapshot(cfg)
    _HEADER_CACHE["snapshot"] = snap
    _HEADER_CACHE["ts"] = now
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
    cursors = snap["server_cursors"]
    if not url:
        server_row = "[red]MIKOSHI_URL not set[/]"
    elif cursors is None:
        server_row = (
            f"[yellow]{url}[/]  [dim]/cursors endpoint unreachable "
            f"(old Mikoshi? Network down? Bad token?)[/]"
        )
    else:
        cache = snap["cache"]
        last_commit = cache.last_successful_commit or "—"
        server_row = (
            f"[green]✓ {url}[/]  "
            f"{len(cursors)} chats tracked   "
            f"last commit {(last_commit or '—')[:19]}"
        )

    summary = pipeline_state.drift_summary(snap["drift"])
    n_local_ahead = summary.get(pipeline_state.DriftStatus.LOCAL_AHEAD, 0)
    n_in_sync = summary.get(pipeline_state.DriftStatus.IN_SYNC, 0)
    n_no_server = summary.get(pipeline_state.DriftStatus.NO_SERVER_RECORD, 0)
    n_no_local = summary.get(pipeline_state.DriftStatus.NO_LOCAL_RECORD, 0)

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

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_row("iPhone", iphone_row)
    table.add_row("Backup", backup_row)
    table.add_row("Decrypt", decrypt_row)
    table.add_row("Server", server_row)
    table.add_row("Sources", _sources_summary_row(snap.get("sources", [])))
    table.add_row("State", state_row)

    console.print(Panel(table, title="[bold cyan]Mikoshi WhatsApp[/]", expand=False))


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
        if not snap["iphone_reachable"]:
            console.print(
                "[red]No iPhone reachable and no cached backup exists.[/]\n"
                "Plug the iPhone (unlock + trust) and try again."
            )
            pause()
            return
        phase = 1
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

    run(cmd, env_extra=env_extra)

    _invalidate_header_cache()
    pause()


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

# Five intent-based top-level actions (REDESIGN.md §5.1).
# The label text includes the cross-reference keyword "Sync" / "Push" / etc.
# in sub-screens so users still find them via the menu's search filter.
ACTIONS = [
    ("🔂  Sync (recommended)",           "sync"),
    ("📊  Inspect (List chats, drift, status)", "inspect"),
    ("📌  Manage favorites",             "favorites"),
    ("⚙   Setup & verify (Verify setup, auth, config)", "setup"),
    ("🛠   Tools (Push, sqlite, advanced)", "tools"),
]

_ACTION_DISPATCH = {
    "sync": action_sync,
    "inspect": action_inspect,
    "favorites": action_favorites,
    "setup": action_setup_verify,
    "tools": action_tools,
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
