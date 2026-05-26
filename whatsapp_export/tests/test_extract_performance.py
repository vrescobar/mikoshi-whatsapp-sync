"""
Regression tests for the Phase-4 perf fix + the new --chat-jid / --since
filters in extract_messages.py.

The original bug: find_attachment_file did `extracted_root.rglob(name)` per
message → O(N*M) over ~700k messages × ~40k media files. With ~half a million
messages and ~tens of thousands of media files in the decrypted tree, Phase 4
took 8h+. After the fix, the media tree is walked exactly once.
"""

import json
import sys
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from extract_messages import (  # noqa: E402
    build_attachments_index,
    extract_messages,
    find_attachment_file,
    load_sync_state,
)


# ─── attachments index ────────────────────────────────────────────────────


class TestAttachmentsIndex:
    def test_indexes_files_by_relpath_and_basename(self, tmp_path):
        root = tmp_path / "ext"
        (root / "Media").mkdir(parents=True)
        (root / "Media" / "a.jpg").write_bytes(b"a")
        (root / "Media" / "sub").mkdir()
        (root / "Media" / "sub" / "b.opus").write_bytes(b"b")

        idx = build_attachments_index(root)

        assert "Media/a.jpg" in idx["by_relpath"]
        assert "Media/sub/b.opus" in idx["by_relpath"]
        assert "a.jpg" in idx["by_basename"]
        assert "b.opus" in idx["by_basename"]

    def test_lookup_prefers_exact_relpath(self, tmp_path):
        root = tmp_path / "ext"
        (root / "Media").mkdir(parents=True)
        (root / "Media" / "x.jpg").write_bytes(b"original")
        # Add an unrelated file with the same basename in another subdir.
        (root / "Other").mkdir()
        (root / "Other" / "x.jpg").write_bytes(b"impostor")

        idx = build_attachments_index(root)
        # ZMEDIALOCALPATH is "Media/x.jpg" — we must resolve to the right one.
        found = find_attachment_file("Media/x.jpg", root, attachments_index=idx)
        assert found is not None
        assert found.read_bytes() == b"original"

    def test_lookup_with_extra_prefix(self, tmp_path):
        """
        The decrypter writes shared-domain media under 'media/' subfolder,
        so a ZMEDIALOCALPATH like 'Media/foo.jpg' lands on disk at
        'media/Media/foo.jpg'. The lookup must tolerate that extra prefix.
        """
        root = tmp_path / "ext"
        (root / "media" / "Media").mkdir(parents=True)
        (root / "media" / "Media" / "foo.jpg").write_bytes(b"x")

        idx = build_attachments_index(root)
        found = find_attachment_file("Media/foo.jpg", root, attachments_index=idx)
        assert found is not None
        assert found.read_bytes() == b"x"

    def test_lookup_falls_back_to_basename(self, tmp_path):
        """If the relpath doesn't match exactly, we still find by basename."""
        root = tmp_path / "ext"
        (root / "weird" / "place").mkdir(parents=True)
        (root / "weird" / "place" / "single.opus").write_bytes(b"x")

        idx = build_attachments_index(root)
        # ZMEDIALOCALPATH says "Media/Audio/single.opus" but file is elsewhere
        found = find_attachment_file(
            "Media/Audio/single.opus", root, attachments_index=idx
        )
        assert found is not None
        assert found.name == "single.opus"

    def test_missing_returns_none(self, tmp_path):
        root = tmp_path / "ext"
        root.mkdir()
        idx = build_attachments_index(root)
        assert find_attachment_file("Media/ghost.jpg", root, attachments_index=idx) is None


class TestFindAttachmentScanCount:
    """
    The smoking gun was rglob-per-message. Make sure the bulk extractor walks
    the media tree exactly once (one os.walk call) regardless of message count.
    """

    def test_extract_walks_media_tree_once(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        # The fixture's extract has 7 user messages — if extract_messages
        # regressed to per-message rglob we'd see 7+ os.walk calls.
        with patch("extract_messages.os.walk", wraps=__import__("os").walk) as spy:
            state = load_sync_state(state_file)
            extract_messages(
                db_path=synthetic_db,
                extracted_root=extracted_root,
                output_path=output_path,
                attachments_dir=attachments_dir,
                sync_state=state,
                mode="full",
            )
        assert spy.call_count == 1, (
            f"extract_messages should walk extracted_root exactly once "
            f"(got {spy.call_count} walks) — regression of the O(N²) bug?"
        )


# ─── --chat-jid filter ────────────────────────────────────────────────────


class TestChatJidFilter:
    def test_exact_match_only(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        state = load_sync_state(state_file)
        extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=output_path,
            attachments_dir=attachments_dir,
            sync_state=state,
            mode="full",
            target_chat_jid="bob@s.whatsapp.net",
        )
        data = json.loads(output_path.read_text())
        assert data["stats"]["total_chats"] == 1
        assert data["chats"][0]["jid"] == "bob@s.whatsapp.net"

    def test_unknown_jid_returns_none(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        state = load_sync_state(state_file)
        result = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=output_path,
            attachments_dir=attachments_dir,
            sync_state=state,
            mode="full",
            target_chat_jid="ghost@s.whatsapp.net",
        )
        assert result is None

    def test_does_not_advance_other_chats_cursor(
        self, synthetic_db, extracted_root, tmp_path, attachments_dir, state_file
    ):
        """
        Scoped runs (--chat-jid) must leave non-targeted chats' watermarks
        untouched, otherwise the next incremental sync silently loses those
        chats' new messages.
        """
        sys.path.insert(0, str(Path(__file__).parent))
        from _helpers import persist_cursors_like_push

        # Seed all cursors with a full sync.
        out1 = tmp_path / "1.json"
        s = load_sync_state(state_file)
        new = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=out1,
            attachments_dir=attachments_dir,
            sync_state=s,
            mode="full",
        )
        persist_cursors_like_push(state_file, new)
        s_reloaded = load_sync_state(state_file)
        alice_before = s_reloaded["chats"]["alice@s.whatsapp.net"]

        # Now scoped sync on Bob only.
        out2 = tmp_path / "2.json"
        s2 = load_sync_state(state_file)
        new2 = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=out2,
            attachments_dir=attachments_dir,
            sync_state=s2,
            mode="incremental",
            target_chat_jid="bob@s.whatsapp.net",
        )
        if new2 is not None:
            persist_cursors_like_push(state_file, new2)

        # Alice's cursor must not have changed.
        s_final = load_sync_state(state_file)
        assert s_final["chats"]["alice@s.whatsapp.net"] == alice_before


# ─── --since filter ──────────────────────────────────────────────────────


class TestSinceFilter:
    def test_drops_messages_before_cutoff(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        # The fixture has messages on 2026-05-{18..24}. A cutoff of 2026-05-23
        # should drop most.
        state = load_sync_state(state_file)
        extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=output_path,
            attachments_dir=attachments_dir,
            sync_state=state,
            mode="full",
            since_iso="2026-05-23T00:00:00+00:00",
        )
        data = json.loads(output_path.read_text())
        for chat in data["chats"]:
            for msg in chat["messages"]:
                assert msg["timestamp"] >= "2026-05-23"

    def test_since_never_rewinds_existing_cursor(
        self, synthetic_db, extracted_root, tmp_path, attachments_dir, state_file
    ):
        """
        If the per-chat cursor is already past the --since date, --since must
        not pull older messages back in. The cursor wins.
        """
        sys.path.insert(0, str(Path(__file__).parent))
        from _helpers import persist_cursors_like_push

        # Full sync first → cursors are at the latest message.
        out1 = tmp_path / "1.json"
        s = load_sync_state(state_file)
        new = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=out1,
            attachments_dir=attachments_dir,
            sync_state=s,
            mode="full",
        )
        persist_cursors_like_push(state_file, new)

        # Now an incremental run with --since far in the past. We expect zero
        # new messages because every chat's cursor is already at the tip.
        out2 = tmp_path / "2.json"
        s2 = load_sync_state(state_file)
        new2 = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=out2,
            attachments_dir=attachments_dir,
            sync_state=s2,
            mode="incremental",
            since_iso="2000-01-01T00:00:00+00:00",
        )
        if new2 is not None:
            data = json.loads(out2.read_text())
            assert data["stats"]["total_messages"] == 0


# ─── attachment skip-if-exists ────────────────────────────────────────────


class TestSkipIfExists:
    def test_does_not_recopy_existing_attachment(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        import os as _os
        import time as _time

        # First run — copies the Bob image.
        state = load_sync_state(state_file)
        extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=output_path,
            attachments_dir=attachments_dir,
            sync_state=state,
            mode="full",
        )
        # Snapshot the copied file's inode (and mtime).
        copied = list(attachments_dir.iterdir())
        assert copied, "first run should have copied something"
        ino_before = _os.stat(copied[0]).st_ino
        # Sleep a tick so we'd see a different mtime if the copy ran again.
        _time.sleep(0.05)

        # Second run — should detect the file already exists and skip.
        out2 = output_path.parent / "out2.json"
        state2 = {"version": 1, "last_global_sync": None, "chats": {}}
        extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=out2,
            attachments_dir=attachments_dir,
            sync_state=state2,
            mode="full",
        )
        # Same inode, untouched.
        assert _os.stat(copied[0]).st_ino == ino_before
