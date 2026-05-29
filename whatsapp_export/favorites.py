"""
Persistent list of favorite WhatsApp chats to sync regularly.

Storage: ~/.mikoshi-favorites.json (or $MIKOSHI_FAVORITES_FILE)

Format (v1, additive):
{
  "version": 1,
  "updated_at": "2026-05-25T22:30:00+00:00",
  "dm_min_messages": 600,   # OPTIONAL rule: auto-include any DM with N+ msgs
  "favorites": [
    {"jid": "34600@s.whatsapp.net", "name": "Alice", "added_at": "..."}
  ]
}

Effective sync set = explicit ``favorites`` ∪ DMs with msg_count ≥
``dm_min_messages`` (resolved against the source DB at sync time).

DMs that already meet the threshold are PRUNED from the explicit list
when the threshold is set — they're redundant because the rule will
re-include them at sync time. Groups (``@g.us``) are NEVER pruned by
the threshold; they only ever appear in the explicit list.

Match is always by JID (stable). ``name`` is only a cache for the UI.
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
        return {
            "version": 1,
            "updated_at": None,
            "dm_min_messages": None,
            "favorites": [],
        }
    data = json.loads(p.read_text())
    # Future-proof: tolerate missing keys
    data.setdefault("version", 1)
    data.setdefault("favorites", [])
    data.setdefault("dm_min_messages", None)
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


def filter_dms_with_min_messages(chats: Iterable[dict], threshold: int) -> list[dict]:
    """Pick 1-on-1 chats with at least ``threshold`` messages.

    Group JIDs (``@g.us``) are excluded — this helper underpins the
    DM-threshold rule, which is explicitly DM-only. Rows missing a
    JID or msg_count are skipped.
    """
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")
    out = []
    for c in chats:
        jid = c.get("jid")
        if not jid or jid.endswith("@g.us"):
            continue
        count = c.get("msg_count")
        if count is None or int(count) < threshold:
            continue
        out.append(c)
    return out


def dm_threshold(file: Path | None = None) -> int | None:
    """Return the persisted DM-message threshold, or None when unset."""
    val = load(file).get("dm_min_messages")
    return int(val) if val is not None else None


def set_dm_threshold(
    threshold: int,
    chats: Iterable[dict],
    file: Path | None = None,
) -> tuple[int, int]:
    """Persist a DM-message threshold and prune redundant DM entries.

    A DM already in the explicit list whose ``msg_count`` is at or above
    the threshold is redundant: the rule will re-include it at sync
    time. Pruning keeps the explicit list focused on the things the
    rule can't infer — groups and below-threshold DMs the user wants
    anyway. Groups (``@g.us``) are NEVER pruned regardless of msg count.

    Returns ``(kept, removed)``.
    """
    if threshold < 1:
        raise ValueError(f"threshold must be >= 1, got {threshold}")
    msg_count_by_jid = {
        c["jid"]: int(c.get("msg_count") or 0)
        for c in chats
        if c.get("jid")
    }
    data = load(file)
    data["dm_min_messages"] = int(threshold)
    before = len(data["favorites"])
    kept = []
    for f in data["favorites"]:
        jid = f.get("jid")
        if not jid:
            continue
        if jid.endswith("@g.us"):
            kept.append(f)
            continue
        # DM — drop if redundant with the rule
        if msg_count_by_jid.get(jid, 0) >= threshold:
            continue
        kept.append(f)
    data["favorites"] = kept
    save(data, file)
    return len(kept), before - len(kept)


def clear_dm_threshold(file: Path | None = None) -> int | None:
    """Remove the threshold rule. Returns the prior threshold (or None)."""
    data = load(file)
    prev = data.get("dm_min_messages")
    if prev is None:
        return None
    data["dm_min_messages"] = None
    save(data, file)
    return int(prev)


def effective_jids(
    chats: Iterable[dict],
    file: Path | None = None,
) -> list[str]:
    """Compute the effective sync set = explicit ∪ threshold-matching DMs.

    ``chats`` should iterate the local ChatStorage rows (each with
    ``jid`` and ``msg_count``). When no threshold is set this just
    returns the explicit JIDs unchanged — ``chats`` is not consumed.
    """
    data = load(file)
    explicit = {f["jid"] for f in data.get("favorites", []) if f.get("jid")}
    threshold = data.get("dm_min_messages")
    if threshold is None:
        return list(explicit)
    for c in chats:
        jid = c.get("jid")
        if not jid or jid.endswith("@g.us"):
            continue
        if int(c.get("msg_count") or 0) >= int(threshold):
            explicit.add(jid)
    return list(explicit)
