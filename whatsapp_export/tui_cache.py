"""On-disk mirror of the TUI's in-memory header snapshot.

The header is expensive to build from scratch — `du -sk` on a growing
iPhone backup, sqlite COUNTs on million-row tables, an HTTP probe to
the Mikoshi server. The in-memory `_HEADER_CACHE` in `tui.py` softens
that within one Python process, but every `./mikoshi-whatsapp.sh tui`
launch starts cold.

This module persists the snapshot so a cold launch can repaint the
header in milliseconds from disk, then refresh in the background.

Storage is plain JSON next to the script. We strip non-JSON-safe
fields (Paths, datetimes, dataclasses) at save time and re-hydrate
the bits the renderer actually consumes at load time. We do **not**
try to round-trip the full snapshot — anything not represented here
just gets re-computed on the next live refresh.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


SOFT_TTL = 30.0
HARD_TTL = 6 * 3600.0
CACHE_VERSION = 1


def cache_path(script_dir: Path) -> Path:
    return script_dir / ".tui_cache.json"


def load(script_dir: Path) -> dict | None:
    """Read the cached snapshot, or None on absent/corrupt/version-bump."""
    path = cache_path(script_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("_version") != CACHE_VERSION:
        return None
    return data


def save(script_dir: Path, snap: dict) -> None:
    """Atomically write a JSON-safe projection of the snapshot.

    Silent on I/O failure — the cache is an optimization, never a
    requirement. Worst case: we recompute next time.
    """
    path = cache_path(script_dir)
    payload = _to_json_safe(snap)
    payload["_version"] = CACHE_VERSION
    payload["_ts"] = time.time()
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def invalidate(script_dir: Path) -> None:
    """Delete the on-disk cache. Called after operations that change state."""
    path = cache_path(script_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def age_seconds(cached: dict | None) -> float:
    if not cached:
        return float("inf")
    ts = cached.get("_ts")
    if not isinstance(ts, (int, float)):
        return float("inf")
    return max(0.0, time.time() - float(ts))


def is_fresh(cached: dict | None) -> bool:
    return age_seconds(cached) < SOFT_TTL


def is_usable(cached: dict | None) -> bool:
    return age_seconds(cached) < HARD_TTL


def _to_json_safe(snap: dict) -> dict[str, Any]:
    """Project the live snapshot into JSON-safe primitives.

    We deliberately keep this shallow and explicit so adding a field
    forces a thought about whether it survives a TUI restart. Fields
    not mentioned here are recomputed at the next live refresh.
    """
    out: dict[str, Any] = {}

    out["iphone_reachable"] = bool(snap.get("iphone_reachable"))
    bdir = snap.get("backup_dir")
    out["backup_dir"] = str(bdir) if bdir else None
    out["backup_udid_count"] = int(snap.get("backup_udid_count") or 0)
    out["backup_size"] = snap.get("backup_size")

    chat = snap.get("chatstorage")
    out["chatstorage"] = str(chat) if chat else None
    cmt = snap.get("chatstorage_mtime")
    out["chatstorage_mtime_iso"] = cmt.isoformat() if cmt is not None else None

    out["server_url"] = snap.get("server_url") or ""
    out["server_cursors_count"] = (
        len(snap["server_cursors"]) if isinstance(snap.get("server_cursors"), dict) else None
    )
    out["server_total_msgs"] = snap.get("server_total_msgs")
    out["local_max_msgs"] = snap.get("local_max_msgs")
    out["last_successful_commit"] = snap.get("last_successful_commit")

    drift = snap.get("drift_summary")
    out["drift_summary"] = drift if isinstance(drift, dict) else None

    out["sources"] = []
    for entry in snap.get("sources") or []:
        e = {
            "name": entry.get("name"),
            "available": bool(entry.get("available")),
            "error": entry.get("error"),
            "snapshot": None,
        }
        s = entry.get("snapshot")
        if s is not None:
            e["snapshot"] = {
                "name": getattr(s, "name", None),
                "db_path": str(getattr(s, "db_path", "")) or None,
                "mtime_iso": getattr(s, "mtime_iso", None),
                "message_count": int(getattr(s, "message_count", 0) or 0),
                "media_with_local_path": int(getattr(s, "media_with_local_path", 0) or 0),
            }
        out["sources"].append(e)

    sched = snap.get("schedule_info")
    if sched:
        out["schedule_info"] = {
            "enabled": bool(sched.get("enabled")),
            "hour": int(sched.get("hour", 0)),
            "minute": int(sched.get("minute", 0)),
            "next_fire_iso": sched.get("next_fire_iso"),
        }
    else:
        out["schedule_info"] = None
    out["last_run_summary"] = snap.get("last_run_summary")

    return out
