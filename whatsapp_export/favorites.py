"""
Persistent list of favorite WhatsApp chats to sync regularly.

Storage: ~/.mikoshi-favorites.json (or $MIKOSHI_FAVORITES_FILE)

Format (v1):
{
  "version": 1,
  "updated_at": "2026-05-25T22:30:00+00:00",
  "favorites": [
    {"jid": "34600@s.whatsapp.net", "name": "Alice", "added_at": "..."}
  ]
}

Match is always by JID (stable). `name` is only a cache for the UI.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_PATH = Path.home() / ".mikoshi-favorites.json"


def path() -> Path:
    return Path(os.environ.get("MIKOSHI_FAVORITES_FILE", str(DEFAULT_PATH)))


def load(file: Path | None = None) -> dict:
    p = file or path()
    if not p.exists():
        return {"version": 1, "updated_at": None, "favorites": []}
    data = json.loads(p.read_text())
    # Future-proof: tolerate missing keys
    data.setdefault("version", 1)
    data.setdefault("favorites", [])
    return data


def save(data: dict, file: Path | None = None) -> None:
    p = file or path()
    data = dict(data)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def jids(file: Path | None = None) -> list[str]:
    """Just the JIDs — handy for SQL IN-list filters."""
    return [f["jid"] for f in load(file).get("favorites", []) if f.get("jid")]


def add(items: Iterable[dict], file: Path | None = None) -> int:
    """
    Add favorites. items: iterable of {"jid": ..., "name": ...}.
    Returns count of new entries (dedup by JID).
    """
    data = load(file)
    existing = {f["jid"] for f in data["favorites"]}
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for item in items:
        jid = item.get("jid")
        if not jid or jid in existing:
            continue
        data["favorites"].append({
            "jid": jid,
            "name": item.get("name") or jid,
            "added_at": now,
        })
        existing.add(jid)
        added += 1
    if added:
        save(data, file)
    return added


def remove(jids_to_remove: Iterable[str], file: Path | None = None) -> int:
    """Remove favorites by JID. Returns count removed."""
    data = load(file)
    drop = set(jids_to_remove)
    before = len(data["favorites"])
    data["favorites"] = [f for f in data["favorites"] if f["jid"] not in drop]
    removed = before - len(data["favorites"])
    if removed:
        save(data, file)
    return removed


def clear(file: Path | None = None) -> int:
    """Wipe all favorites. Returns count removed."""
    data = load(file)
    n = len(data["favorites"])
    if n:
        data["favorites"] = []
        save(data, file)
    return n
