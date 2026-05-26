"""
Shared state helpers used by every entry point.

Imported from:
  - tui.py            — status header, drift display, plan screen
  - extract_messages.py — read cursors only; never write (M3+)
  - push_via_api.py   — write cursor cache after a successful commit
  - mikoshi-whatsapp.sh sync — shells out to `python3 -m pipeline_state best-phase`
                                to share `_best_from_phase` with the TUI

Responsibilities
----------------
1. Cursor cache I/O. The local `.sync_state.json` is a *cache* of the
   server's per-chat commit cursors. Writing it is the privilege of
   exactly one place: push_via_api.commit_and_persist(). Reads happen
   from anywhere.

2. Drift detection. After this redesign the client and the server can
   each carry a per-chat watermark; the rule is "server wins". This
   module compares the two and labels each chat IN_SYNC / LOCAL_AHEAD
   / SERVER_AHEAD / NO_SERVER_RECORD.

3. Server cursor fetch. Hits `GET /api/ingest/v1/cursors`. Returns
   `None` on 404 / 5xx / timeout — older Mikoshi installs don't expose
   the endpoint, and we degrade silently to local-only behaviour with
   a warning surfaced to the UI layer.

4. `_best_from_phase` — pick the cheapest --from-phase based on on-disk
   state. Used by both TUI and cron-driven sync (closes pain point #9).

5. Plan computation. Pure SQL COUNT against ChatStorage.sqlite, bounded
   by server cursors. Lets the TUI's plan screen tell the user
   "1,247 new messages across 3 chats" before any real work happens.

The module has zero hard dependencies on questionary / rich / Mikoshi
client libraries — only stdlib + sqlite3 + urllib. That keeps it
importable from the cron path even when the venv isn't activated for
display tooling.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


IOS_EPOCH_OFFSET = 978307200  # 2001-01-01 UTC

_SQLITE_MAGIC = b"SQLite format 3\x00"

# Source label written into v2 cache entries. Distinguishes server-confirmed
# cursors from values that were ever stamped from local extraction alone
# (the historical mode that caused the silent-drift bug).
SOURCE_SERVER = "server"
SOURCE_EXTRACTED_LEGACY = "extracted (legacy)"
SOURCE_EXTRACTED_OFFLINE = "extracted (offline)"


# ─── data classes ─────────────────────────────────────────────────────────


@dataclass
class ChatCursor:
    """One chat's commit watermark."""
    committed_through_ts: str | None = None        # ISO-8601, UTC
    committed_through_external_id: str | None = None  # "ios:<Z_PK>"
    source: str = SOURCE_SERVER

    def iso_to_ios(self) -> float:
        return iso_to_ios_ts(self.committed_through_ts)


@dataclass
class CursorCache:
    """In-memory representation of .sync_state.json (v2)."""
    version: int = 2
    server_url: str | None = None
    last_cursor_refresh: str | None = None
    last_successful_commit: str | None = None
    last_push_id: str | None = None
    chats: dict[str, ChatCursor] = field(default_factory=dict)

    def cutoff_ios(self, jid: str) -> float:
        """The iOS cutoff to feed to extract — i.e. messages strictly newer than this."""
        c = self.chats.get(jid)
        return c.iso_to_ios() if c else 0.0


class DriftStatus(str, Enum):
    IN_SYNC = "in_sync"                # local ts == server ts (or both None)
    LOCAL_AHEAD = "local_ahead"        # the bug: local thinks "synced", server has less
    SERVER_AHEAD = "server_ahead"      # cache stale; refresh from server
    NO_SERVER_RECORD = "no_server"     # never pushed; local may have an extracted-legacy entry
    NO_LOCAL_RECORD = "no_local"       # fresh local, server already has something


@dataclass
class DriftEntry:
    jid: str
    status: DriftStatus
    local_ts: str | None
    server_ts: str | None
    note: str = ""


@dataclass
class ChatPlanEntry:
    jid: str
    name: str | None
    cutoff_ts: str | None      # the timestamp we're starting from (max of local cache vs server cursor)
    new_messages: int          # rows past the cutoff
    new_attachments: int       # rows past the cutoff that have a ZWAMEDIAITEM


@dataclass
class Plan:
    scope: str                 # "all" / "favorites" / "one-chat"
    chats: list[ChatPlanEntry]
    server_endpoint_present: bool   # if False, plan was computed against local cache only

    @property
    def total_messages(self) -> int:
        return sum(c.new_messages for c in self.chats)

    @property
    def total_attachments(self) -> int:
        return sum(c.new_attachments for c in self.chats)


# ─── timestamp helpers ────────────────────────────────────────────────────


def ios_to_iso(ios_ts: float | None) -> str | None:
    if ios_ts is None:
        return None
    try:
        unix = float(ios_ts) + IOS_EPOCH_OFFSET
        return datetime.fromtimestamp(unix, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def iso_to_ios_ts(iso_str: str | None) -> float:
    if not iso_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.timestamp() - IOS_EPOCH_OFFSET
    except (ValueError, TypeError):
        return 0.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── SQLite header check ──────────────────────────────────────────────────


def looks_like_sqlite(path: Path) -> bool:
    """Cheap "is this file actually a SQLite DB?" check.

    A killed Phase 3 leaves the output file size-extended but with the
    first page still zero — `path.stat().st_size > 0` lies. Reading the
    16-byte magic header catches that without opening the DB at all.
    """
    try:
        with path.open("rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


# ─── cache I/O ────────────────────────────────────────────────────────────


def load_cursor_cache(state_file: Path) -> CursorCache:
    """Load .sync_state.json. Tolerant of v1 (legacy) and v2 shapes.

    Never raises on a missing or corrupt file — returns an empty cache.
    A corrupt file is renamed to .sync_state.json.broken-<ts> on first
    sight so subsequent successful writes can land cleanly.
    """
    if not state_file.exists():
        return CursorCache()

    try:
        raw = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        # Quarantine the broken file. Don't delete — the user might want
        # to inspect it manually.
        try:
            state_file.rename(state_file.with_suffix(
                state_file.suffix + ".broken-" + now_iso().replace(":", "").replace("-", "")[:14]
            ))
        except OSError:
            pass
        return CursorCache()

    version = int(raw.get("version", 1) or 1)
    if version >= 2:
        cache = CursorCache(
            version=version,
            server_url=raw.get("server_url"),
            last_cursor_refresh=raw.get("last_cursor_refresh"),
            last_successful_commit=raw.get("last_successful_commit"),
            last_push_id=raw.get("last_push_id"),
        )
        for jid, entry in (raw.get("chats") or {}).items():
            if isinstance(entry, dict):
                cache.chats[jid] = ChatCursor(
                    committed_through_ts=entry.get("committed_through_ts"),
                    committed_through_external_id=entry.get("committed_through_external_id"),
                    source=entry.get("source") or SOURCE_SERVER,
                )
            else:
                # Mixed file: a v2 envelope with v1-style values.
                cache.chats[jid] = ChatCursor(
                    committed_through_ts=str(entry) if entry else None,
                    source=SOURCE_EXTRACTED_LEGACY,
                )
        return cache

    # v1 → v2 migration in-memory. Don't write yet — only successful commits
    # are allowed to write the cache from M3 onward.
    cache = CursorCache(
        version=2,
        last_successful_commit=raw.get("last_global_sync"),
    )
    for jid, iso_ts in (raw.get("chats") or {}).items():
        cache.chats[jid] = ChatCursor(
            committed_through_ts=str(iso_ts) if iso_ts else None,
            source=SOURCE_EXTRACTED_LEGACY,
        )
    return cache


def save_cursor_cache(state_file: Path, cache: CursorCache) -> None:
    """Atomically write the cache to disk in v2 format."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    cache.version = 2  # force v2 on every write
    payload: dict[str, Any] = {
        "version": 2,
        "server_url": cache.server_url,
        "last_cursor_refresh": cache.last_cursor_refresh,
        "last_successful_commit": cache.last_successful_commit,
        "last_push_id": cache.last_push_id,
        "chats": {jid: asdict(c) for jid, c in cache.chats.items()},
    }
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(state_file)


def update_cache_from_commit(
    state_file: Path,
    server_url: str,
    push_id: str | None,
    committed_cursors: dict[str, dict[str, str]],
) -> CursorCache:
    """Called from push_via_api after `commit` returns 200.

    `committed_cursors` is the per-JID dict returned by the server,
    shape: `{jid: {ts: ISO, external_id: "ios:<pk>"}}`.

    When the server omits this block (older Mikoshi without the M2 PR),
    callers pass `committed_cursors={}` and rely on
    `update_cache_from_extraction_fallback` to populate from the manifest.
    """
    cache = load_cursor_cache(state_file)
    cache.server_url = server_url
    cache.last_successful_commit = now_iso()
    cache.last_push_id = push_id

    for jid, entry in (committed_cursors or {}).items():
        cache.chats[jid] = ChatCursor(
            committed_through_ts=entry.get("ts"),
            committed_through_external_id=entry.get("external_id"),
            source=SOURCE_SERVER,
        )
    save_cursor_cache(state_file, cache)
    return cache


def update_cache_from_extraction_fallback(
    state_file: Path,
    manifest_path: Path,
    server_url: str,
    push_id: str | None,
) -> CursorCache:
    """Fallback when the server didn't echo `committed_cursors`.

    Reads the manifest we just pushed (idempotently committed on the
    server) and stamps the local cache with the highest external_id +
    timestamp per chat from the manifest. Source label flags these as
    OFFLINE so drift detection can re-verify against the server next time.
    """
    cache = load_cursor_cache(state_file)
    cache.server_url = server_url
    cache.last_successful_commit = now_iso()
    cache.last_push_id = push_id

    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        save_cursor_cache(state_file, cache)
        return cache

    for chat in manifest.get("chats", []):
        jid = chat.get("jid")
        if not jid:
            continue
        # Pick the max-timestamp message — manifests are sorted ASC but
        # we can't trust that.
        best_ts: str | None = None
        best_ext: str | None = None
        for msg in chat.get("messages", []):
            ts = msg.get("timestamp")
            if ts is None:
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_ext = msg.get("external_id")
        if best_ts is None:
            continue
        # If the new value is older than the existing one (e.g. partial
        # re-push), keep the existing one — never rewind.
        existing = cache.chats.get(jid)
        if existing and existing.committed_through_ts and existing.committed_through_ts >= best_ts:
            continue
        cache.chats[jid] = ChatCursor(
            committed_through_ts=best_ts,
            committed_through_external_id=best_ext,
            source=SOURCE_EXTRACTED_OFFLINE,
        )
    save_cursor_cache(state_file, cache)
    return cache


# ─── server cursor fetch ──────────────────────────────────────────────────


def fetch_server_cursors(
    url: str,
    token: str,
    timeout: float = 3.0,
) -> dict[str, ChatCursor] | None:
    """
    Hit `GET /api/ingest/v1/cursors`. Return a dict of JID→ChatCursor.

    Returns `None` (not an empty dict!) when:
      - the endpoint doesn't exist (404)         — old Mikoshi
      - auth fails (401)                          — bad token
      - network/timeout error                     — server unreachable

    The distinction matters: an empty dict means "server confirmed it
    has nothing yet" (so a first sync should push everything);
    `None` means "we couldn't determine, fall back to local cache."
    """
    if not url or not token:
        return None
    full = url.rstrip("/") + "/api/ingest/v1/cursors"
    req = urllib.request.Request(full, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        # 401/403/5xx — also fall back rather than crashing the UI.
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return None
    cursors: dict[str, ChatCursor] = {}
    # Accept either {jid: {ts, external_id}} or {chats: {jid: {ts, external_id}}}.
    payload = data.get("chats", data) if isinstance(data, dict) else {}
    for jid, entry in (payload or {}).items():
        if not isinstance(entry, dict):
            continue
        cursors[jid] = ChatCursor(
            committed_through_ts=entry.get("ts") or entry.get("timestamp"),
            committed_through_external_id=entry.get("external_id"),
            source=SOURCE_SERVER,
        )
    return cursors


# ─── drift detection ──────────────────────────────────────────────────────


def detect_drift(
    cache: CursorCache,
    server: dict[str, ChatCursor] | None,
) -> list[DriftEntry]:
    """
    Compute the per-chat drift report.

    `server=None` means we couldn't reach the server (old Mikoshi /
    network down). In that case we still emit one entry per local chat
    with status NO_SERVER_RECORD so the UI can flag uncertainty.
    """
    drift: list[DriftEntry] = []

    if server is None:
        for jid, c in sorted(cache.chats.items()):
            drift.append(DriftEntry(
                jid=jid,
                status=DriftStatus.NO_SERVER_RECORD,
                local_ts=c.committed_through_ts,
                server_ts=None,
                note="server endpoint unreachable",
            ))
        return drift

    all_jids = set(cache.chats.keys()) | set(server.keys())
    for jid in sorted(all_jids):
        local = cache.chats.get(jid)
        srv = server.get(jid)
        local_ts = local.committed_through_ts if local else None
        srv_ts = srv.committed_through_ts if srv else None
        if not local and srv:
            status = DriftStatus.NO_LOCAL_RECORD
            note = "server has commits this client doesn't know about"
        elif local and not srv:
            status = DriftStatus.NO_SERVER_RECORD
            note = "local cursor exists but server has no commits — likely from a failed-push run"
        elif local_ts == srv_ts:
            status = DriftStatus.IN_SYNC
            note = ""
        elif local_ts and srv_ts and local_ts > srv_ts:
            status = DriftStatus.LOCAL_AHEAD
            note = "local thinks 'synced' but server has older cursor (the silent-drift bug)"
        else:
            status = DriftStatus.SERVER_AHEAD
            note = "another client pushed; refresh local cache"
        drift.append(DriftEntry(jid=jid, status=status, local_ts=local_ts, server_ts=srv_ts, note=note))

    return drift


def drift_summary(report: list[DriftEntry]) -> dict[DriftStatus, int]:
    out: dict[DriftStatus, int] = {s: 0 for s in DriftStatus}
    for e in report:
        out[e.status] += 1
    return out


# ─── _best_from_phase (shared with cron) ──────────────────────────────────


def find_udid_dirs(backup_dir: Path) -> list[Path]:
    """UDID directories are >20 chars long. Skip Apple Mac Backup folders, etc."""
    backup_root = backup_dir / "backup"
    if not backup_root.exists():
        return []
    return [d for d in backup_root.iterdir() if d.is_dir() and len(d.name) > 20]


def best_from_phase(backup_dir: Path | None) -> tuple[int, str]:
    """
    Pick the cheapest --from-phase based on what's already on disk.

    Phase semantics in the *redesigned* pipeline:
      1 — no usable backup at all → need iPhone connected
      3 — encrypted backup exists, ChatStorage isn't decrypted (or invalid)
      4 — decrypted ChatStorage exists → extract-only, seconds

    Lives here (not in tui.py) so the cron path in mikoshi-whatsapp.sh
    can ask the same question and pick the same answer instead of always
    starting from Phase 1 and failing when the iPhone isn't around.
    """
    if not backup_dir:
        return 1, "Refresh from iPhone (incremental — fetches only new data)"

    udids = find_udid_dirs(backup_dir)
    encrypted_ok = any(
        (d / "Manifest.plist").exists() and (d / "Manifest.plist").stat().st_size > 0
        for d in udids
    )

    chat_db = backup_dir / "extracted" / "ChatStorage.sqlite"
    decrypted_ok = chat_db.exists() and looks_like_sqlite(chat_db)

    if decrypted_ok:
        return 4, "Extract-only (seconds, reuses decrypted DB)"
    if encrypted_ok:
        return 3, "Re-decrypt existing backup (~30 min, no iPhone)"
    return 1, "Refresh from iPhone (incremental — fetches only new data)"


def device_reachable(timeout: float = 5.0) -> bool:
    """Quick probe: is an iPhone visible via libimobiledevice?

    Used by both the TUI header and cron sync. Failure modes are all
    "not reachable" — we don't try to distinguish "no device" from
    "device locked" here; the pipeline does that.
    """
    import shutil
    import subprocess

    if not shutil.which("idevice_id"):
        return False
    try:
        result = subprocess.run(
            ["idevice_id", "-l"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


# ─── plan computation ─────────────────────────────────────────────────────


def _media_size_expr(cur: sqlite3.Cursor) -> str:
    """Same trick as extract_messages._resolve_media_size_expr — handle ZFILESIZE/ZMEDIASIZE schema variance."""
    rows = cur.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()
    cols = {row[1].upper() for row in rows}
    if "ZFILESIZE" in cols and "ZMEDIASIZE" in cols:
        return "COALESCE(mi.ZFILESIZE, mi.ZMEDIASIZE)"
    if "ZFILESIZE" in cols:
        return "mi.ZFILESIZE"
    if "ZMEDIASIZE" in cols:
        return "mi.ZMEDIASIZE"
    return "NULL"


def compute_plan(
    db_path: Path,
    cache: CursorCache,
    server: dict[str, ChatCursor] | None,
    scope_jids: set[str] | None = None,
) -> Plan:
    """
    Without extracting anything, count how many messages would be pushed
    if we ran the pipeline right now.

    The cutoff per chat is `max(local_cache_ts, server_ts)` — server wins
    when present, local cache fills the gap when not. This is the same
    rule extract_messages.py applies in incremental mode.

    `scope_jids=None` = "all chats". Anything else restricts to that set.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    _ = _media_size_expr(cur)  # not used in COUNT but proves the table exists

    chats = cur.execute(
        """
        SELECT Z_PK as chat_pk,
               ZCONTACTJID as jid,
               ZPARTNERNAME as name
          FROM ZWACHATSESSION
         WHERE ZCONTACTJID IS NOT NULL
        """
    ).fetchall()

    entries: list[ChatPlanEntry] = []
    for chat in chats:
        jid = chat["jid"]
        if scope_jids is not None and jid not in scope_jids:
            continue

        local_ts = cache.cutoff_ios(jid)
        server_ts = 0.0
        server_iso: str | None = None
        if server and jid in server:
            server_iso = server[jid].committed_through_ts
            server_ts = iso_to_ios_ts(server_iso)
        # Server wins when present.
        cutoff_ios_ts = server_ts if server_iso is not None else local_ts
        cutoff_iso = server_iso or (cache.chats.get(jid).committed_through_ts if cache.chats.get(jid) else None)

        msg_count = cur.execute(
            "SELECT COUNT(*) FROM ZWAMESSAGE WHERE ZCHATSESSION = ? AND ZMESSAGEDATE > ?",
            (chat["chat_pk"], cutoff_ios_ts),
        ).fetchone()[0]
        att_count = cur.execute(
            """
            SELECT COUNT(*) FROM ZWAMESSAGE m
              JOIN ZWAMEDIAITEM mi ON mi.ZMESSAGE = m.Z_PK
             WHERE m.ZCHATSESSION = ? AND m.ZMESSAGEDATE > ?
               AND mi.ZMEDIALOCALPATH IS NOT NULL
            """,
            (chat["chat_pk"], cutoff_ios_ts),
        ).fetchone()[0]
        entries.append(ChatPlanEntry(
            jid=jid,
            name=chat["name"],
            cutoff_ts=cutoff_iso,
            new_messages=int(msg_count or 0),
            new_attachments=int(att_count or 0),
        ))

    conn.close()

    scope_label = "all" if scope_jids is None else (
        "one-chat" if scope_jids and len(scope_jids) == 1 else "favorites"
    )
    return Plan(
        scope=scope_label,
        chats=sorted(entries, key=lambda e: -e.new_messages),
        server_endpoint_present=server is not None,
    )


# ─── tiny CLI so bash can call this without a full Python wrapper ─────────


def _main(argv: list[str] | None = None) -> int:
    """Internal CLI — used by mikoshi-whatsapp.sh to share `best_from_phase`
    with the cron path. Stdout is a single short string. Stderr is human."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_best = sub.add_parser("best-phase", help="Print best --from-phase given on-disk state")
    p_best.add_argument("--backup-dir", type=Path, help="Override MIKOSHI_BACKUP_DIR")
    p_best.add_argument("--require-iphone", action="store_true",
                        help="Bail (exit 2) when phase 1 would be required but no iPhone is reachable")

    p_drift = sub.add_parser("drift", help="Print drift summary as JSON")
    p_drift.add_argument("--state-file", type=Path, required=True)
    p_drift.add_argument("--url")
    p_drift.add_argument("--token")

    args = parser.parse_args(argv)

    if args.cmd == "best-phase":
        backup_dir = args.backup_dir or (
            Path(os.environ["MIKOSHI_BACKUP_DIR"])
            if os.environ.get("MIKOSHI_BACKUP_DIR") else None
        )
        phase, label = best_from_phase(backup_dir)
        if phase == 1 and args.require_iphone and not device_reachable():
            print("1\tno iPhone reachable", file=sys.stderr)
            return 2
        # stdout: <phase>\t<label>  — easy to parse in bash with `read`.
        print(f"{phase}\t{label}")
        return 0

    if args.cmd == "drift":
        cache = load_cursor_cache(args.state_file)
        srv = fetch_server_cursors(args.url or "", args.token or "")
        report = detect_drift(cache, srv)
        summary = {s.value: n for s, n in drift_summary(report).items()}
        print(json.dumps({
            "summary": summary,
            "entries": [asdict(e) | {"status": e.status.value} for e in report],
        }, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
