#!/usr/bin/env python3
"""
WhatsApp message extractor with incremental sync support.

Extracts messages, attachments (images, audio, docs <5MB) from
decrypted iOS WhatsApp backup. Supports incremental sync based on
last sync timestamp and full-contact mode.

Filters:
  - Skip video files (any size)
  - Skip attachments > 5MB
  - Skip WhatsApp system messages (group events, etc.) unless --include-system

Sync modes:
  - incremental (default): only messages since last sync per chat
  - full-contact <name>: full history for a specific contact
  - full: full history (reset sync state)
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# iOS Core Data reference epoch: 2001-01-01 00:00:00 UTC
IOS_EPOCH_OFFSET = 978307200

MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024  # 5MB

ALLOWED_MIME_PREFIXES = (
    "image/",
    "audio/",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "text/",
)

REJECTED_MIME_PREFIXES = ("video/",)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp"}

# ZWAMESSAGE.ZMESSAGETYPE values that represent system events, not user content.
# Sourced from WhatsApp iOS internals — extend as we observe new types.
SYSTEM_MESSAGE_TYPES = {
    6,   # group event: name change, icon change, member add/remove, etc.
    7,   # call placeholder (not the user's own typed message)
    8,   # E2E encryption notice
    10,  # group creation
    11,  # group settings change
    14,  # contact card change
    15,  # business / verified-business notice
    16,  # disappearing-messages notice
    19,  # group invite link change
    27,  # security-code change
}


def ios_timestamp_to_iso(ts):
    if ts is None:
        return None
    try:
        epoch = float(ts) + IOS_EPOCH_OFFSET
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def iso_to_ios_timestamp(iso_str):
    if not iso_str:
        return 0.0
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.timestamp() - IOS_EPOCH_OFFSET


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_sync_state(state_file):
    if not state_file.exists():
        return {"version": 1, "last_global_sync": None, "chats": {}}
    with open(state_file) as f:
        return json.load(f)


def save_sync_state(state_file, state):
    state["last_global_sync"] = datetime.now(timezone.utc).isoformat()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(state_file)


def attachment_is_allowed(mime, file_path):
    mime = (mime or "").lower()

    if mime.startswith(REJECTED_MIME_PREFIXES):
        return False, "video filtered"

    if file_path and file_path.suffix.lower() in VIDEO_EXTENSIONS:
        return False, "video extension filtered"

    if file_path and file_path.exists():
        size = file_path.stat().st_size
        if size > MAX_ATTACHMENT_SIZE:
            return False, f"size {size} > {MAX_ATTACHMENT_SIZE}"

    if mime and not mime.startswith(ALLOWED_MIME_PREFIXES):
        # Unknown MIME — let it through; size already capped above
        pass

    return True, "ok"


def build_attachments_index(extracted_root):
    """
    Walk extracted_root ONCE and build two lookup tables.

    - `by_relpath`: maps the WhatsApp-internal media path (the value stored in
      ZWAMEDIAITEM.ZMEDIALOCALPATH, e.g. "Media/abc/xyz.jpg") to its absolute
      path on disk. Exact-match, unambiguous.
    - `by_basename`: maps just the filename to a list of absolute paths.
      Fallback for the (rare) case where ZMEDIALOCALPATH on iOS doesn't
      match how the file landed under `extracted/`.

    Building the index once is the difference between O(N) and O(N²) lookups
    over hundreds of thousands of messages. Before this refactor each message
    triggered an `rglob` over the entire decrypted media tree.
    """
    by_relpath: dict[str, str] = {}
    by_basename: dict[str, list[str]] = {}
    if not extracted_root.exists():
        return {"by_relpath": by_relpath, "by_basename": by_basename}
    root_str = str(extracted_root)
    root_len = len(root_str.rstrip(os.sep)) + 1
    for dirpath, _dirnames, filenames in os.walk(extracted_root):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            # Relative path from extracted_root, normalised to forward slashes
            # so it can match WhatsApp's "Media/foo/bar.jpg" style.
            rel = full[root_len:].replace(os.sep, "/")
            by_relpath[rel] = full
            by_basename.setdefault(fname, []).append(full)
    return {"by_relpath": by_relpath, "by_basename": by_basename}


def find_attachment_file(media_local_path, extracted_root, attachments_index=None):
    """
    Resolve ZMEDIALOCALPATH to an on-disk file.

    Prefers the prebuilt `attachments_index` (O(1) dict lookup) when provided.
    Falls back to a one-shot rglob ONLY when no index is supplied — this path
    exists for callers that don't go through the bulk extractor (tests,
    scripting), and is never hit during a normal pipeline run.
    """
    if not media_local_path:
        return None
    target_rel = str(media_local_path).replace(os.sep, "/").lstrip("/")
    target_name = Path(media_local_path).name

    if attachments_index is not None:
        by_relpath = attachments_index.get("by_relpath", {})
        by_basename = attachments_index.get("by_basename", {})

        # Exact relpath match — the happy path for current WhatsApp iOS
        # backups where ZMEDIALOCALPATH and the decrypted tree layout agree.
        if target_rel in by_relpath:
            return Path(by_relpath[target_rel])

        # The decrypter may write WhatsApp shared media under an extra
        # prefix (e.g. "media/Media/foo.jpg" when the relpath in the DB is
        # just "Media/foo.jpg"). Try the suffix match.
        for rel, full in by_relpath.items():
            if rel.endswith("/" + target_rel) or rel == target_rel:
                return Path(full)

        # Last resort: any file with the same basename.
        candidates = by_basename.get(target_name)
        if candidates:
            return Path(candidates[0])
        return None

    # No index — fallback for tests / one-off callers.
    candidates = list(extracted_root.rglob(target_name))
    return candidates[0] if candidates else None


def table_exists(cursor, table_name):
    row = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def fetch_group_participants(cursor, chat_pk):
    """
    Return list of group participants for a chat. Empty for 1-1 chats.
    Schema varies across WhatsApp versions, so guard table existence.
    """
    if not table_exists(cursor, "ZWAGROUPMEMBER"):
        return []
    try:
        rows = cursor.execute(
            """
            SELECT
                ZMEMBERJID as jid,
                ZCONTACTNAME as name
            FROM ZWAGROUPMEMBER
            WHERE ZCHATSESSION = ?
            """,
            (chat_pk,),
        ).fetchall()
        return [
            {"jid": r["jid"], "name": r["name"]}
            for r in rows
            if r["jid"]
        ]
    except sqlite3.OperationalError:
        return []


def is_group_chat(jid):
    return bool(jid) and jid.endswith("@g.us")


def _resolve_media_size_expr(cursor) -> str:
    """
    WhatsApp iOS has used different column names for attachment size across
    versions: ZMEDIASIZE on older builds, ZFILESIZE on newer ones, and some
    builds carry both. Inspect ZWAMEDIAITEM to build an expression that
    works against whichever schema this backup uses.

    Returns the SQL fragment to substitute for `mi.<media_size_col> as media_size`.
    Picks a sensible NULL if neither column exists.
    """
    rows = cursor.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()
    cols = {row["name"].upper() for row in rows}
    has_filesize = "ZFILESIZE" in cols
    has_mediasize = "ZMEDIASIZE" in cols
    if has_filesize and has_mediasize:
        return "COALESCE(mi.ZFILESIZE, mi.ZMEDIASIZE) as media_size"
    if has_filesize:
        return "mi.ZFILESIZE as media_size"
    if has_mediasize:
        return "mi.ZMEDIASIZE as media_size"
    # Neither — pipeline doesn't actually need this value anyway (size_bytes
    # is recomputed from the file on disk), so a NULL placeholder is safe.
    return "NULL as media_size"


def extract_messages(
    db_path,
    extracted_root,
    output_path,
    attachments_dir,
    sync_state,
    mode="incremental",
    target_contact=None,
    target_chat_jid=None,
    since_iso=None,
    include_system=False,
    favorite_jids=None,
):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    media_size_expr = _resolve_media_size_expr(cursor)

    chats_query = """
        SELECT
            Z_PK as chat_pk,
            ZCONTACTJID as jid,
            ZPARTNERNAME as name,
            ZLASTMESSAGEDATE as last_msg_ts
        FROM ZWACHATSESSION
        WHERE ZCONTACTJID IS NOT NULL
    """
    params: tuple = ()
    if target_chat_jid:
        # Exact-match filter — preferred when the caller already has a JID
        # (e.g. from /memory/scopes flows or a previous run). Unlike
        # --contact this never matches partial names.
        chats_query += " AND ZCONTACTJID = ?"
        params = (target_chat_jid,)
        print(f"[INFO] Filtering to chat JID {target_chat_jid}")
    elif mode == "full-contact" and target_contact:
        chats_query += " AND (ZPARTNERNAME LIKE ? OR ZCONTACTJID LIKE ?)"
        params = (f"%{target_contact}%", f"%{target_contact}%")

    if favorite_jids:
        placeholders = ",".join("?" * len(favorite_jids))
        chats_query += f" AND ZCONTACTJID IN ({placeholders})"
        params = params + tuple(favorite_jids)
        print(f"[INFO] Filtering to {len(favorite_jids)} favorite(s)")

    chats = cursor.execute(chats_query, params).fetchall()

    if not chats:
        print("[ERROR] No chats found", file=sys.stderr)
        if target_contact:
            print(f"[ERROR] Searched for contact: {target_contact}", file=sys.stderr)
        if favorite_jids:
            print(f"[ERROR] None of the {len(favorite_jids)} favorite JIDs matched any chat",
                  file=sys.stderr)
        return None

    if favorite_jids and len(chats) < len(favorite_jids):
        missing = set(favorite_jids) - {c["jid"] for c in chats}
        print(f"[WARN] {len(missing)} favorite JID(s) not found in this backup: "
              f"{', '.join(list(missing)[:3])}{'...' if len(missing) > 3 else ''}",
              file=sys.stderr)

    print(f"[INFO] Processing {len(chats)} chat(s)")

    attachments_dir.mkdir(parents=True, exist_ok=True)
    new_state = dict(sync_state.get("chats", {}))

    # Build the attachments index ONCE for the whole run. Was the smoking gun
    # behind the ~5-9h Phase-4 projection: each message used to trigger an
    # rglob over the entire decrypted media tree (40k+ files), turning the
    # extractor into an O(N*M) operation. Now it's O(N+M).
    print(f"[INFO] Indexing decrypted media tree under {extracted_root}…")
    _idx_start = datetime.now(timezone.utc)
    attachments_index = build_attachments_index(extracted_root)
    _idx_files = len(attachments_index.get("by_relpath", {}))
    _idx_elapsed = (datetime.now(timezone.utc) - _idx_start).total_seconds()
    print(f"[INFO] Indexed {_idx_files} file(s) in {_idx_elapsed:.1f}s")

    # since_iso → iOS cutoff (clamped against per-chat cursors).
    since_ios = iso_to_ios_timestamp(since_iso) if since_iso else 0.0
    if since_iso:
        print(f"[INFO] Applying --since cutoff {since_iso} (iOS ts {since_ios:.0f})")

    export = {
        "schema_version": "1.2",
        "client_id": os.environ.get("MIKOSHI_CLIENT_ID", platform.node() or "macos-client"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "target_contact": target_contact,
        "target_chat_jid": target_chat_jid,
        "since": since_iso,
        "include_system_messages": include_system,
        "chats": [],
    }

    total_msgs = 0
    total_system_skipped = 0
    total_attachments = 0
    skipped_attachments = 0

    for chat in chats:
        chat_jid = chat["jid"]
        chat_pk = chat["chat_pk"]
        chat_name = chat["name"] or chat_jid

        if mode == "incremental":
            last_ts_iso = sync_state.get("chats", {}).get(chat_jid)
            cutoff_ios = iso_to_ios_timestamp(last_ts_iso) if last_ts_iso else 0.0
        else:
            cutoff_ios = 0.0

        # --since acts as a *lower bound* relative to the chat cursor: a user
        # asking "give me everything since 2024-01-01" never wants messages
        # older than that, even if the per-chat cursor hasn't been set yet.
        # Conversely we never *rewind* the cursor with --since.
        if since_ios and since_ios > cutoff_ios:
            cutoff_ios = since_ios

        messages = cursor.execute(
            """
            SELECT
                m.Z_PK as msg_pk,
                m.ZTEXT as text,
                m.ZMESSAGEDATE as msg_ts,
                m.ZFROMJID as from_jid,
                m.ZTOJID as to_jid,
                m.ZISFROMME as is_from_me,
                m.ZMESSAGETYPE as msg_type,
                m.ZPUSHNAME as push_name,
                mi.Z_PK as media_pk,
                mi.ZMEDIALOCALPATH as media_path,
                mi.ZVCARDSTRING as media_mime,
                mi.ZTITLE as media_title,
                """ + media_size_expr + """
            FROM ZWAMESSAGE m
            LEFT JOIN ZWAMEDIAITEM mi ON mi.ZMESSAGE = m.Z_PK
            WHERE m.ZCHATSESSION = ? AND m.ZMESSAGEDATE > ?
            ORDER BY m.ZMESSAGEDATE ASC
            """,
            (chat_pk, cutoff_ios),
        ).fetchall()

        if not messages:
            continue

        participants = fetch_group_participants(cursor, chat_pk) if is_group_chat(chat_jid) else []

        chat_obj = {
            "jid": chat_jid,
            "name": chat_name,
            "is_group": is_group_chat(chat_jid),
            "participants": participants,
            "messages": [],
        }

        latest_ts_in_chat = cutoff_ios

        for m in messages:
            msg_ts = m["msg_ts"] or 0.0
            if msg_ts > latest_ts_in_chat:
                latest_ts_in_chat = msg_ts

            msg_type = m["msg_type"]
            if not include_system and msg_type in SYSTEM_MESSAGE_TYPES:
                total_system_skipped += 1
                # still advance the cursor — we don't want to keep re-visiting it
                continue

            msg_obj = {
                "id": m["msg_pk"],
                "external_id": f"ios:{m['msg_pk']}",
                "timestamp": ios_timestamp_to_iso(msg_ts),
                "from_jid": m["from_jid"],
                "to_jid": m["to_jid"],
                "is_from_me": bool(m["is_from_me"]),
                "push_name": m["push_name"],
                "text": m["text"],
                "type": msg_type,
                "attachment": None,
            }

            if m["media_pk"]:
                source_file = find_attachment_file(
                    m["media_path"], extracted_root, attachments_index=attachments_index
                )
                allowed, reason = attachment_is_allowed(m["media_mime"], source_file)

                if not allowed:
                    skipped_attachments += 1
                    msg_obj["attachment"] = {
                        "skipped": True,
                        "reason": reason,
                        "mime": m["media_mime"],
                        "original_path": m["media_path"],
                    }
                elif source_file and source_file.exists():
                    file_hash = sha256_file(source_file)
                    ext = source_file.suffix or ".bin"
                    dest_name = f"{file_hash}{ext}"
                    dest = attachments_dir / dest_name
                    # Skip-if-exists with size check — preserves the ~40k
                    # attachments already on disk from earlier (killed) runs.
                    src_size = source_file.stat().st_size
                    if dest.exists() and dest.stat().st_size == src_size:
                        pass  # already there from a prior run
                    else:
                        shutil.copy2(source_file, dest)

                    total_attachments += 1
                    msg_obj["attachment"] = {
                        "skipped": False,
                        "sha256": file_hash,
                        "filename": dest_name,
                        "mime": m["media_mime"],
                        "size_bytes": src_size,
                        "title": m["media_title"],
                    }

            chat_obj["messages"].append(msg_obj)
            total_msgs += 1
            if total_msgs % 5000 == 0:
                print(f"[INFO] …progress: {total_msgs} messages processed", flush=True)

        export["chats"].append(chat_obj)

        # Advance the watermark only for chats we actually processed in this
        # run. Both `full-contact` and `--chat-jid` are *scoped* runs by
        # design — they must not touch cursors for chats they didn't touch,
        # or the next incremental sync will silently skip messages.
        scoped_run = mode == "full-contact" or target_chat_jid is not None
        if (not scoped_run) or chat_jid == chat["jid"]:
            new_state[chat_jid] = ios_timestamp_to_iso(latest_ts_in_chat)

        print(
            f"[INFO] {chat_name}: {len(chat_obj['messages'])} message(s)"
            + (f", {len(participants)} participant(s)" if participants else "")
        )

    export["stats"] = {
        "total_chats": len(export["chats"]),
        "total_messages": total_msgs,
        "system_messages_skipped": total_system_skipped,
        "attachments_kept": total_attachments,
        "attachments_skipped": skipped_attachments,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, ensure_ascii=False, default=str)

    conn.close()

    print(f"[INFO] {total_msgs} messages, {total_attachments} attachments → {output_path}")
    print(f"[INFO] Skipped: {skipped_attachments} attachments, {total_system_skipped} system messages")

    return new_state


def main():
    parser = argparse.ArgumentParser(description="WhatsApp extractor for Mikoshi")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--extracted-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--attachments-dir", required=True, type=Path)
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument(
        "--mode", choices=["incremental", "full", "full-contact"], default="incremental"
    )
    parser.add_argument("--contact", help="Required when --mode=full-contact")
    parser.add_argument(
        "--chat-jid",
        help="Restrict extraction to this exact ZCONTACTJID. Stronger and "
             "more predictable than --contact (which does a substring match "
             "against name and JID).",
    )
    parser.add_argument(
        "--since",
        help="Only emit messages whose timestamp is >= this ISO-8601 date "
             "(e.g. 2026-01-01). Combines with the per-chat watermark: "
             "we never rewind, but we never go further back than --since "
             "either.",
    )
    parser.add_argument(
        "--include-system",
        action="store_true",
        help="Include WhatsApp system messages (group events, encryption notices, etc.)",
    )
    parser.add_argument(
        "--favorites-file",
        type=Path,
        help="Path to favorites JSON; if set, restrict extraction to those JIDs. "
             "Empty list aborts with exit 2.",
    )

    args = parser.parse_args()

    if args.mode == "full-contact" and not args.contact:
        parser.error("--contact required when --mode=full-contact")

    if not args.db.exists():
        print(f"[ERROR] DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    favorite_jids = None
    if args.favorites_file:
        # Defer import so the rest of the script doesn't depend on favorites.py
        import favorites as _favs
        favorite_jids = _favs.jids(args.favorites_file)
        if not favorite_jids:
            print(f"[ERROR] Favorites file has no entries: {args.favorites_file}",
                  file=sys.stderr)
            sys.exit(2)

    sync_state = load_sync_state(args.state_file)

    if args.mode == "full":
        print("[INFO] Full sync mode: resetting state")
        sync_state = {"version": 1, "last_global_sync": None, "chats": {}}

    new_chats_state = extract_messages(
        db_path=args.db,
        extracted_root=args.extracted_root,
        output_path=args.output,
        attachments_dir=args.attachments_dir,
        sync_state=sync_state,
        mode=args.mode,
        target_contact=args.contact,
        target_chat_jid=args.chat_jid,
        since_iso=args.since,
        include_system=args.include_system,
        favorite_jids=favorite_jids,
    )

    if new_chats_state is None:
        sys.exit(2)

    sync_state["chats"] = new_chats_state
    save_sync_state(args.state_file, sync_state)
    print(f"[INFO] Sync state saved: {args.state_file}")


if __name__ == "__main__":
    main()
