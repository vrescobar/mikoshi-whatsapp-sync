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

import pipeline_state

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
    """
    Read the cursor cache and project it into the v1-shape dict this
    module's internals were originally built around: ``{"chats": {jid: iso_ts}}``.

    The on-disk file may be either v1 (pre-redesign) or v2 (server-confirmed
    after a successful commit). `pipeline_state.load_cursor_cache` handles
    both shapes transparently. The projection back to v1 lets the rest of
    this script (and existing tests) keep working unchanged.
    """
    cache = pipeline_state.load_cursor_cache(state_file)
    return {
        "version": 1,
        "last_global_sync": cache.last_successful_commit,
        "chats": {
            jid: c.committed_through_ts
            for jid, c in cache.chats.items()
            if c.committed_through_ts
        },
    }


def save_sync_state(state_file, state):
    """
    Write cursors back to disk.

    From the redesign onward this is a NO-OP unless the user explicitly
    opted into the legacy behaviour via ``MIKOSHI_TRUST_LOCAL_CURSOR=1``.
    The reason it's gated: writing cursors at extraction time is what
    caused the silent-drift bug — a 401 on push left local cursors
    pretending the server had data it never received. The single
    authorised cursor writer is now ``push_via_api.commit`` after a 200
    from ``/commit``.

    When the escape hatch is on, we still write — but in the v2 schema,
    with ``source: "extracted (legacy)"`` so drift detection knows these
    values must be re-verified against the server.
    """
    if os.environ.get("MIKOSHI_TRUST_LOCAL_CURSOR", "").strip().lower() not in ("1", "true", "yes", "on"):
        print(
            "[INFO] Skipping cursor write — push_via_api.py will advance cursors after a successful commit.",
            file=sys.stderr,
        )
        print(
            "[INFO]   (set MIKOSHI_TRUST_LOCAL_CURSOR=1 to restore the legacy extraction-time write.)",
            file=sys.stderr,
        )
        return

    print(
        "[WARN] MIKOSHI_TRUST_LOCAL_CURSOR=1 is set — writing cursors at extraction time.",
        file=sys.stderr,
    )
    print(
        "[WARN]   This re-enables the pre-redesign behaviour where cursors can advance past a failed push.",
        file=sys.stderr,
    )
    cache = pipeline_state.load_cursor_cache(state_file)
    cache.last_successful_commit = state.get("last_global_sync") or pipeline_state.now_iso()
    for jid, iso_ts in (state.get("chats") or {}).items():
        if not iso_ts:
            continue
        existing = cache.chats.get(jid)
        if existing and existing.committed_through_ts and existing.committed_through_ts >= str(iso_ts):
            # Don't rewind a cursor someone else (push) already advanced past.
            continue
        cache.chats[jid] = pipeline_state.ChatCursor(
            committed_through_ts=str(iso_ts),
            source=pipeline_state.SOURCE_EXTRACTED_LEGACY,
        )
    pipeline_state.save_cursor_cache(state_file, cache)


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
    dm_min_messages=None,
):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    media_size_expr = _resolve_media_size_expr(cursor)

    # Expand favorite_jids with the DM-threshold rule before building the
    # chats query. The rule means "any 1-on-1 chat with N+ messages": it's
    # resolved per-DB so each source contributes whatever currently meets
    # the bar (the Mac live DB has shorter history and may include fewer).
    if dm_min_messages is not None:
        threshold_rows = cursor.execute("""
            SELECT s.ZCONTACTJID
            FROM ZWACHATSESSION s
            LEFT JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
            WHERE s.ZCONTACTJID IS NOT NULL
              AND s.ZCONTACTJID NOT LIKE '%@g.us'
            GROUP BY s.Z_PK
            HAVING COUNT(m.Z_PK) >= ?
        """, (int(dm_min_messages),)).fetchall()
        threshold_jids = {row[0] for row in threshold_rows if row[0]}
        if threshold_jids:
            existing = set(favorite_jids or [])
            unioned = existing | threshold_jids
            new_from_rule = unioned - existing
            favorite_jids = sorted(unioned)
            print(
                f"[INFO] DM threshold ≥{dm_min_messages} adds "
                f"{len(new_from_rule)} chat(s) to the favorites filter "
                f"({len(threshold_jids)} matched the rule)"
            )

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
                m.ZSTANZAID as stanza_id,
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

            # Dual external_id: `wa:<ZSTANZAID>` is stable across sources
            # (same value on iPhone backup and Mac live DB for the same
            # logical message). `legacy_external_id` carries the older
            # `ios:<Z_PK>` so a Mikoshi that already received the legacy
            # form can dedup on it instead of inserting a duplicate.
            # Falls back to ios:<Z_PK> as the primary id for the ~7 rows
            # in 300k where ZSTANZAID is null (group-system messages).
            stanza_id = m["stanza_id"]
            legacy_id = f"ios:{m['msg_pk']}"
            external_id = f"wa:{stanza_id}" if stanza_id else legacy_id

            msg_obj = {
                "id": m["msg_pk"],
                "external_id": external_id,
                "legacy_external_id": legacy_id if external_id != legacy_id else None,
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


def extract_messages_multi_source(
    sources,
    output_path,
    attachments_dir,
    sync_state,
    mode="incremental",
    target_contact=None,
    target_chat_jid=None,
    since_iso=None,
    include_system=False,
    favorite_jids=None,
    dm_min_messages=None,
):
    """Run extraction against N sources, reconcile, write one manifest.

    Calls ``extract_messages`` once per source (each writes its own
    transient JSON to a temp file), then merges the results with
    ``reconciler.reconcile`` into a single per-chat-deduped manifest at
    ``output_path``.

    ``sources`` is a list of ``sources.base.Source`` instances; each
    must be available (``s.is_available() == True``). Cursor / state
    semantics match single-source extraction: per-chat cursors are the
    union of "what each source has seen", but the returned
    ``new_state`` is only used for the legacy MIKOSHI_TRUST_LOCAL_CURSOR
    path — the server's commit response is still the canonical update
    in the redesign.
    """
    import tempfile

    from reconciler import reconcile

    if not sources:
        raise ValueError("extract_messages_multi_source: at least one source required")

    per_source_chats: dict[str, dict[str, list[dict]]] = {}
    per_source_state: dict[str, dict[str, str]] = {}
    per_source_attachments_per_chat: dict[str, dict[str, list[dict]]] = {}

    # Each source extracts into its own temp output so we can read both
    # back as native manifest dicts and run them through the reconciler.
    for source in sources:
        with tempfile.NamedTemporaryFile(
            prefix=f"mikoshi-extract-{source.name}-",
            suffix=".json",
            delete=False,
            dir=str(output_path.parent),
        ) as tf:
            tmp_path = Path(tf.name)
        per_source_attachments_dir = attachments_dir  # share — all sha256-keyed
        new_state = extract_messages(
            db_path=source.db_path(),
            extracted_root=source.media_root() or source.db_path().parent,
            output_path=tmp_path,
            attachments_dir=per_source_attachments_dir,
            sync_state=sync_state,
            mode=mode,
            target_contact=target_contact,
            target_chat_jid=target_chat_jid,
            since_iso=since_iso,
            include_system=include_system,
            favorite_jids=favorite_jids,
            dm_min_messages=dm_min_messages,
        )
        if new_state is None:
            # Source had nothing matching the filter — skip cleanly.
            tmp_path.unlink(missing_ok=True)
            continue
        with open(tmp_path, encoding="utf-8") as f:
            payload = json.load(f)
        tmp_path.unlink(missing_ok=True)
        per_source_chats[source.name] = {
            chat["jid"]: chat["messages"] for chat in payload["chats"]
        }
        per_source_attachments_per_chat[source.name] = {
            chat["jid"]: chat for chat in payload["chats"]
        }
        per_source_state[source.name] = new_state

    if not per_source_chats:
        return None

    # Run the dedup. Order: iphone before mac (the iPhone backup is the
    # media authority — its rows win attachment-provenance ties).
    merged_chats = reconcile(
        per_source_chats,
        source_order=["iphone_backup", "mac_live"],
    )

    # Carry chat metadata (name, is_group, participants) from whichever
    # source described the chat — prefer iphone_backup, fall back to mac_live.
    merged_state: dict[str, str] = {}
    for jid, msgs in merged_chats.items():
        for st in per_source_state.values():
            if jid in st:
                # Latest timestamp wins across sources.
                if jid not in merged_state or (st[jid] or "") > merged_state[jid]:
                    merged_state[jid] = st[jid]

    output_chats = []
    total_msgs = 0
    total_attachments = 0
    skipped_attachments = 0
    for jid, msgs in merged_chats.items():
        meta = None
        for src_name in ("iphone_backup", "mac_live"):
            if src_name in per_source_attachments_per_chat:
                meta = per_source_attachments_per_chat[src_name].get(jid)
                if meta:
                    break
        if not meta:
            continue
        chat_out = {
            "jid": jid,
            "name": meta.get("name"),
            "is_group": meta.get("is_group", False),
            "participants": meta.get("participants", []),
            "messages": msgs,
        }
        output_chats.append(chat_out)
        total_msgs += len(msgs)
        for m in msgs:
            att = m.get("attachment")
            if att and att.get("skipped") is False:
                total_attachments += 1
            elif att and att.get("skipped") is True:
                skipped_attachments += 1

    # Reuse the manifest envelope of the first source's output.
    export = {
        "schema_version": "1.2",
        "client_id": os.environ.get("MIKOSHI_CLIENT_ID", platform.node() or "macos-client"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "target_contact": target_contact,
        "target_chat_jid": target_chat_jid,
        "since": since_iso,
        "include_system_messages": include_system,
        "sources": sorted(per_source_chats.keys()),
        "chats": output_chats,
        "stats": {
            "total_chats": len(output_chats),
            "total_messages": total_msgs,
            "attachments_kept": total_attachments,
            "attachments_skipped": skipped_attachments,
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, ensure_ascii=False, default=str)

    print(
        f"[INFO] multi-source: {total_msgs} messages across {len(output_chats)} chats "
        f"after reconcile (sources: {', '.join(sorted(per_source_chats.keys()))})"
    )
    return merged_state


def main():
    parser = argparse.ArgumentParser(description="WhatsApp extractor for Mikoshi")
    # `--db` and `--extracted-root` are required for single-source mode.
    # `--sources` selects multi-source mode (paths are discovered from
    # the `sources/` registry rather than passed in).
    parser.add_argument("--db", type=Path, help="Single-source: path to ChatStorage.sqlite")
    parser.add_argument("--extracted-root", type=Path, help="Single-source: media tree root")
    parser.add_argument(
        "--sources",
        help="Comma-separated list of source names (e.g. 'iphone_backup,mac_live'). "
             "When set, takes precedence over --db/--extracted-root; the chosen "
             "sources are merged and deduped via reconciler.reconcile.",
    )
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

    if not args.sources and not args.db:
        parser.error("either --sources or --db is required")

    if args.sources and (args.db or args.extracted_root):
        parser.error("--sources is mutually exclusive with --db / --extracted-root")

    if args.db and not args.db.exists():
        print(f"[ERROR] DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    favorite_jids = None
    dm_min_messages = None
    if args.favorites_file:
        # Defer import so the rest of the script doesn't depend on favorites.py
        import favorites as _favs
        favorite_jids = _favs.jids(args.favorites_file)
        dm_min_messages = _favs.dm_threshold(args.favorites_file)
        # Either an explicit list OR a threshold rule is enough — the
        # rule alone will be resolved against each source DB downstream.
        if not favorite_jids and dm_min_messages is None:
            print(f"[ERROR] Favorites file has no entries: {args.favorites_file}",
                  file=sys.stderr)
            sys.exit(2)

    sync_state = load_sync_state(args.state_file)

    if args.mode == "full":
        print("[INFO] Full sync mode: resetting state")
        sync_state = {"version": 1, "last_global_sync": None, "chats": {}}

    if args.sources:
        from sources import get_source
        source_names = [s.strip() for s in args.sources.split(",") if s.strip()]
        source_objs = []
        for name in source_names:
            try:
                src = get_source(name)
            except KeyError:
                print(f"[ERROR] unknown source: {name}", file=sys.stderr)
                sys.exit(1)
            if not src.is_available():
                print(f"[WARN] source {name} not available on this Mac — skipping",
                      file=sys.stderr)
                continue
            source_objs.append(src)
        if not source_objs:
            print("[ERROR] no requested sources are available", file=sys.stderr)
            sys.exit(2)
        new_chats_state = extract_messages_multi_source(
            sources=source_objs,
            output_path=args.output,
            attachments_dir=args.attachments_dir,
            sync_state=sync_state,
            mode=args.mode,
            target_contact=args.contact,
            target_chat_jid=args.chat_jid,
            since_iso=args.since,
            include_system=args.include_system,
            favorite_jids=favorite_jids,
            dm_min_messages=dm_min_messages,
        )
    else:
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
            dm_min_messages=dm_min_messages,
        )

    if new_chats_state is None:
        sys.exit(2)

    # `new_chats_state` is the cursor map *would-be* — we hand it to the
    # push step via the manifest, and push_via_api will persist the
    # authoritative version after a successful commit. The local save
    # below is a no-op by default (see save_sync_state docstring), kept
    # only as a manual escape hatch.
    sync_state["chats"] = new_chats_state
    save_sync_state(args.state_file, sync_state)


if __name__ == "__main__":
    main()
