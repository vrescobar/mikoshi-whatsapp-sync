"""Tests for extract_messages.py — incremental/full/full-contact + filters."""

import json
from datetime import datetime, timezone

import pytest

from extract_messages import (
    IOS_EPOCH_OFFSET,
    MAX_ATTACHMENT_SIZE,
    attachment_is_allowed,
    extract_messages,
    ios_timestamp_to_iso,
    iso_to_ios_timestamp,
    load_sync_state,
    sha256_file,
)


# ─── Pure helpers ──────────────────────────────────────────────────────────

class TestTimestamps:
    def test_roundtrip(self):
        # 2026-05-25 12:00 UTC
        original = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc).timestamp() - IOS_EPOCH_OFFSET
        iso = ios_timestamp_to_iso(original)
        back = iso_to_ios_timestamp(iso)
        assert abs(back - original) < 0.001

    def test_none_in_returns_none(self):
        assert ios_timestamp_to_iso(None) is None

    def test_empty_string_returns_zero(self):
        assert iso_to_ios_timestamp("") == 0.0
        assert iso_to_ios_timestamp(None) == 0.0


class TestAttachmentFilters:
    def test_video_mime_rejected(self, tmp_path):
        f = tmp_path / "x.mp4"
        f.write_bytes(b"x")
        allowed, reason = attachment_is_allowed("video/mp4", f)
        assert not allowed
        assert "video" in reason

    def test_video_extension_rejected(self, tmp_path):
        f = tmp_path / "x.mp4"
        f.write_bytes(b"x")
        allowed, reason = attachment_is_allowed(None, f)
        assert not allowed

    def test_oversize_rejected(self, tmp_path):
        f = tmp_path / "big.pdf"
        f.write_bytes(b"x" * (MAX_ATTACHMENT_SIZE + 1))
        allowed, reason = attachment_is_allowed("application/pdf", f)
        assert not allowed
        assert "size" in reason

    def test_image_kept(self, tmp_path):
        f = tmp_path / "a.jpg"
        f.write_bytes(b"x" * 1024)
        allowed, reason = attachment_is_allowed("image/jpeg", f)
        assert allowed

    def test_audio_kept(self, tmp_path):
        f = tmp_path / "a.opus"
        f.write_bytes(b"x" * 1024)
        allowed, _ = attachment_is_allowed("audio/opus", f)
        assert allowed

    def test_pdf_under_5mb_kept(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x" * 1024)
        allowed, _ = attachment_is_allowed("application/pdf", f)
        assert allowed


class TestSha256:
    def test_consistent(self, tmp_path):
        f = tmp_path / "x"
        f.write_bytes(b"hello world")
        assert sha256_file(f) == sha256_file(f)
        assert len(sha256_file(f)) == 64


class TestSyncState:
    def test_missing_returns_default(self, tmp_path):
        state = load_sync_state(tmp_path / "nope.json")
        assert state == {"version": 1, "last_global_sync": None, "chats": {}}


# ─── End-to-end extraction ─────────────────────────────────────────────────

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent))
from _helpers import persist_cursors_like_push as _persist_cursors_like_push  # noqa: E402


def run_extract(db, root, out, atts, state_file, **kwargs):
    """Helper: load state, extract, persist state-as-if-push-succeeded."""
    state = load_sync_state(state_file)
    if kwargs.get("mode") == "full":
        state = {"version": 1, "last_global_sync": None, "chats": {}}

    new_chats_state = extract_messages(
        db_path=db,
        extracted_root=root,
        output_path=out,
        attachments_dir=atts,
        sync_state=state,
        mode=kwargs.get("mode", "incremental"),
        target_contact=kwargs.get("target_contact"),
        include_system=kwargs.get("include_system", False),
        favorite_jids=kwargs.get("favorite_jids"),
    )
    if new_chats_state is not None:
        _persist_cursors_like_push(state_file, new_chats_state)
    return json.loads(out.read_text())


class TestFullSync:
    def test_extracts_all_chats(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        # 3 chats expected
        assert result["stats"]["total_chats"] == 3
        # 3 + 2 + 2 = 7 user messages (system 401 filtered by default)
        assert result["stats"]["total_messages"] == 7
        assert result["stats"]["system_messages_skipped"] == 1

    def test_schema_version_is_current(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        assert result["schema_version"] == "1.2"
        # v1.2 adds client_id (audit trail). external_id is `wa:<stanza>`
        # when the row has ZSTANZAID, or the legacy `ios:<pk>` form when
        # stanza is null (one row in the synthetic fixture exercises that).
        assert result["client_id"]
        for chat in result["chats"]:
            for msg in chat["messages"]:
                wa_form = msg["external_id"].startswith("wa:")
                legacy_form = msg["external_id"] == f"ios:{msg['id']}"
                assert wa_form or legacy_form, (
                    f"unexpected external_id {msg['external_id']!r} for msg {msg['id']}"
                )
                if wa_form:
                    # The dual id carries the prior ios: form too.
                    assert msg["legacy_external_id"] == f"ios:{msg['id']}"
                else:
                    # Legacy-only path: no legacy_external_id (would be redundant).
                    assert msg["legacy_external_id"] is None


class TestIncrementalSync:
    def test_second_run_returns_nothing_new(
        self, synthetic_db, extracted_root, tmp_path, attachments_dir, state_file
    ):
        # First run — full
        first = tmp_path / "first.json"
        run_extract(synthetic_db, extracted_root, first, attachments_dir, state_file,
                    mode="full")

        # Second run — incremental
        second = tmp_path / "second.json"
        result = run_extract(synthetic_db, extracted_root, second, attachments_dir, state_file,
                             mode="incremental")
        assert result["stats"]["total_messages"] == 0
        assert result["stats"]["total_chats"] == 0  # no chat had new messages


class TestFullContactSync:
    def test_only_targets_named_contact(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full-contact", target_contact="Alice",
        )
        assert result["stats"]["total_chats"] == 1
        assert result["chats"][0]["name"] == "Alice"

    def test_state_unchanged_for_other_chats(
        self, synthetic_db, extracted_root, tmp_path, attachments_dir, state_file
    ):
        # Initial full sync sets all cursors
        out1 = tmp_path / "1.json"
        run_extract(synthetic_db, extracted_root, out1, attachments_dir, state_file, mode="full")
        # Use load_sync_state so we get the v1-shape projection regardless
        # of whether the on-disk file is v1 or v2 — the new cache writes
        # v2 but load_sync_state collapses it back to {jid: iso_ts}.
        state_after_full = load_sync_state(state_file)

        # full-contact on Alice should NOT touch Bob's cursor
        out2 = tmp_path / "2.json"
        run_extract(synthetic_db, extracted_root, out2, attachments_dir, state_file,
                    mode="full-contact", target_contact="Alice")
        state_after_contact = load_sync_state(state_file)

        assert state_after_full["chats"]["bob@s.whatsapp.net"] == \
               state_after_contact["chats"]["bob@s.whatsapp.net"]


class TestAttachments:
    def test_image_attached_and_copied(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        # Find Bob's chat
        bob = next(c for c in result["chats"] if c["name"] == "Bob")
        media_msg = next(m for m in bob["messages"] if m["attachment"])
        assert media_msg["attachment"]["skipped"] is False
        assert media_msg["attachment"]["sha256"]
        # File copied
        copied = attachments_dir / media_msg["attachment"]["filename"]
        assert copied.exists()

    def test_video_attachment_skipped(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        # Find group chat
        group = next(c for c in result["chats"] if c["is_group"])
        video_msg = next((m for m in group["messages"] if m["attachment"]), None)
        # Either skipped or filtered
        assert video_msg is not None
        assert video_msg["attachment"]["skipped"] is True


class TestGroupParticipants:
    def test_group_chat_has_participants(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        group = next(c for c in result["chats"] if c["is_group"])
        assert len(group["participants"]) == 3
        jids = {p["jid"] for p in group["participants"]}
        assert "alice@s.whatsapp.net" in jids

    def test_one_to_one_no_participants(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        alice = next(c for c in result["chats"] if c["name"] == "Alice")
        assert alice["is_group"] is False
        assert alice["participants"] == []


class TestSystemMessages:
    def test_system_filtered_by_default(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        group = next(c for c in result["chats"] if c["is_group"])
        # Group has 1 system msg (type 10) — should NOT appear
        types = [m["type"] for m in group["messages"]]
        assert 10 not in types

    def test_include_system_keeps_them(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full", include_system=True,
        )
        group = next(c for c in result["chats"] if c["is_group"])
        types = [m["type"] for m in group["messages"]]
        assert 10 in types


class TestSchemaConformance:
    def test_output_validates_against_schema(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        import jsonschema
        from pathlib import Path as _P

        run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
        )
        schema_path = _P(__file__).parent.parent / "schema.json"
        schema = json.loads(schema_path.read_text())
        data = json.loads(output_path.read_text())
        jsonschema.Draft7Validator(schema).validate(data)


# ─── Favorites filter ──────────────────────────────────────────────────────

class TestFavoritesFilter:
    def test_filters_to_jid_list(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        # Restrict to Bob only — Alice and group should be excluded
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
            favorite_jids=["bob@s.whatsapp.net"],
        )
        assert result["stats"]["total_chats"] == 1
        assert result["chats"][0]["jid"] == "bob@s.whatsapp.net"

    def test_multiple_jids(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
            favorite_jids=["alice@s.whatsapp.net", "12345@g.us"],
        )
        jids = {c["jid"] for c in result["chats"]}
        assert jids == {"alice@s.whatsapp.net", "12345@g.us"}

    def test_unknown_jid_silently_skipped(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        # Bob exists, the other one doesn't — should still succeed with just Bob
        result = run_extract(
            synthetic_db, extracted_root, output_path, attachments_dir, state_file,
            mode="full",
            favorite_jids=["bob@s.whatsapp.net", "ghost@s.whatsapp.net"],
        )
        assert result["stats"]["total_chats"] == 1
        assert result["chats"][0]["jid"] == "bob@s.whatsapp.net"

    def test_all_unknown_jids_returns_none(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        from extract_messages import extract_messages, load_sync_state
        state = load_sync_state(state_file)
        result = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=output_path,
            attachments_dir=attachments_dir,
            sync_state=state,
            mode="full",
            favorite_jids=["ghost1@s.whatsapp.net", "ghost2@s.whatsapp.net"],
        )
        assert result is None  # extract_messages signals "nothing extracted"


class TestFavoritesModule:
    def test_add_and_remove(self, tmp_path):
        import favorites
        f = tmp_path / "favs.json"

        added = favorites.add(
            [
                {"jid": "alice@s.whatsapp.net", "name": "Alice"},
                {"jid": "bob@s.whatsapp.net", "name": "Bob"},
            ],
            file=f,
        )
        assert added == 2
        assert set(favorites.jids(f)) == {"alice@s.whatsapp.net", "bob@s.whatsapp.net"}

        # Re-adding Alice is a no-op
        again = favorites.add([{"jid": "alice@s.whatsapp.net", "name": "Alice"}], file=f)
        assert again == 0

        removed = favorites.remove(["bob@s.whatsapp.net"], file=f)
        assert removed == 1
        assert favorites.jids(f) == ["alice@s.whatsapp.net"]

    def test_load_missing_file(self, tmp_path):
        import favorites
        data = favorites.load(tmp_path / "nonexistent.json")
        assert data == {"version": 1, "updated_at": None, "favorites": []}

    def test_clear(self, tmp_path):
        import favorites
        f = tmp_path / "favs.json"
        favorites.add([{"jid": "x@s.whatsapp.net", "name": "X"}], file=f)
        assert favorites.clear(f) == 1
        assert favorites.jids(f) == []


# ─── Edge cases: malformed / empty DB ──────────────────────────────────────

class TestEmptyDB:
    """Real backups can have weird states. Don't crash."""

    def _build_empty_db(self, db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZLASTMESSAGEDATE REAL
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZTEXT TEXT,
                ZMESSAGEDATE REAL,
                ZFROMJID TEXT,
                ZTOJID TEXT,
                ZISFROMME INTEGER,
                ZMESSAGETYPE INTEGER,
                ZPUSHNAME TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZMEDIALOCALPATH TEXT,
                ZVCARDSTRING TEXT,
                ZTITLE TEXT,
                ZMEDIASIZE INTEGER
            );
        """)
        conn.commit()
        conn.close()

    def test_empty_db_returns_none(self, tmp_path, attachments_dir, state_file):
        from extract_messages import extract_messages, load_sync_state
        db = tmp_path / "empty.sqlite"
        self._build_empty_db(db)
        out = tmp_path / "out.json"
        result = extract_messages(
            db_path=db,
            extracted_root=tmp_path / "root",
            output_path=out,
            attachments_dir=attachments_dir,
            sync_state=load_sync_state(state_file),
            mode="full",
        )
        assert result is None  # no chats found

    def test_missing_ZWAGROUPMEMBER_table_doesnt_crash(
        self, synthetic_db, extracted_root, output_path, attachments_dir, state_file
    ):
        """Old DB versions lack ZWAGROUPMEMBER. fetch_group_participants() must guard."""
        import sqlite3
        from extract_messages import extract_messages, load_sync_state

        # Drop the group-member table from the synthetic_db fixture
        conn = sqlite3.connect(synthetic_db)
        conn.execute("DROP TABLE ZWAGROUPMEMBER")
        conn.commit()
        conn.close()

        result = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=output_path,
            attachments_dir=attachments_dir,
            sync_state=load_sync_state(state_file),
            mode="full",
        )
        assert result is not None
        # Group chats should appear but with empty participants list
        data = json.loads(output_path.read_text())
        groups = [c for c in data["chats"] if c["is_group"]]
        assert all(g["participants"] == [] for g in groups)


class TestNullValues:
    """Handle NULL text, NULL timestamp, NULL push_name without crashing."""

    def test_null_text_message(self, tmp_path, attachments_dir, state_file):
        import sqlite3
        from extract_messages import extract_messages, load_sync_state

        db = tmp_path / "null.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZLASTMESSAGEDATE REAL
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER,
                ZTEXT TEXT,
                ZMESSAGEDATE REAL,
                ZFROMJID TEXT,
                ZTOJID TEXT,
                ZISFROMME INTEGER,
                ZMESSAGETYPE INTEGER,
                ZPUSHNAME TEXT,
                ZSTANZAID TEXT
            );
            CREATE TABLE ZWAMEDIAITEM (
                Z_PK INTEGER PRIMARY KEY,
                ZMESSAGE INTEGER,
                ZMEDIALOCALPATH TEXT,
                ZVCARDSTRING TEXT,
                ZTITLE TEXT,
                ZMEDIASIZE INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'x@s.whatsapp.net', NULL, 100.0);
            INSERT INTO ZWAMESSAGE VALUES (1, 1, NULL, 50.0, 'x@s.whatsapp.net', 'me@s.whatsapp.net', 0, 0, NULL, NULL);
        """)
        conn.commit()
        conn.close()

        out = tmp_path / "out.json"
        result = extract_messages(
            db_path=db,
            extracted_root=tmp_path / "root",
            output_path=out,
            attachments_dir=attachments_dir,
            sync_state=load_sync_state(state_file),
            mode="full",
        )
        assert result is not None
        data = json.loads(out.read_text())
        msg = data["chats"][0]["messages"][0]
        assert msg["text"] is None
        assert msg["push_name"] is None
        # Chat name falls back to JID
        assert data["chats"][0]["name"] == "x@s.whatsapp.net"


class TestSchemaCompatibility:
    """Different WhatsApp iOS versions name the attachment size column
    differently. extract_messages should query the schema at runtime."""

    def _db_with_cols(self, tmp_path, cols):
        """Build a DB whose ZWAMEDIAITEM has exactly `cols` (plus Z_PK and
        the always-present columns extract_messages reads)."""
        import sqlite3
        db = tmp_path / "schema.sqlite"
        always = "Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, " \
                 "ZMEDIALOCALPATH TEXT, ZVCARDSTRING TEXT, ZTITLE TEXT"
        extra = ", ".join(f"{c} INTEGER" for c in cols)
        defn = always + (", " + extra if extra else "")
        conn = sqlite3.connect(db)
        conn.execute(f"CREATE TABLE ZWAMEDIAITEM ({defn})")
        conn.commit()
        return db

    def test_picks_zfilesize_when_only_filesize(self, tmp_path):
        from extract_messages import _resolve_media_size_expr
        import sqlite3
        db = self._db_with_cols(tmp_path, ["ZFILESIZE"])
        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            expr = _resolve_media_size_expr(c.cursor())
        assert expr == "mi.ZFILESIZE as media_size"

    def test_picks_zmediasize_when_only_mediasize(self, tmp_path):
        from extract_messages import _resolve_media_size_expr
        import sqlite3
        db = self._db_with_cols(tmp_path, ["ZMEDIASIZE"])
        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            expr = _resolve_media_size_expr(c.cursor())
        assert expr == "mi.ZMEDIASIZE as media_size"

    def test_coalesces_when_both_present(self, tmp_path):
        from extract_messages import _resolve_media_size_expr
        import sqlite3
        db = self._db_with_cols(tmp_path, ["ZFILESIZE", "ZMEDIASIZE"])
        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            expr = _resolve_media_size_expr(c.cursor())
        assert expr == "COALESCE(mi.ZFILESIZE, mi.ZMEDIASIZE) as media_size"

    def test_falls_back_to_null_when_neither(self, tmp_path):
        from extract_messages import _resolve_media_size_expr
        import sqlite3
        db = self._db_with_cols(tmp_path, [])
        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            expr = _resolve_media_size_expr(c.cursor())
        assert expr == "NULL as media_size"


class TestStateOverlapWithFavorites:
    """The state cursor should only advance for JIDs we actually processed."""

    def test_favorites_dont_touch_other_chats_cursors(
        self, synthetic_db, extracted_root, tmp_path, attachments_dir, state_file
    ):
        from extract_messages import extract_messages, load_sync_state
        # Full sync first to seed all cursors. Persist via the same path
        # push_via_api would use on commit success (the new cursor-write rule).
        out1 = tmp_path / "1.json"
        state = load_sync_state(state_file)
        s1 = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=out1,
            attachments_dir=attachments_dir,
            sync_state=state,
            mode="full",
        )
        _persist_cursors_like_push(state_file, s1)
        state = load_sync_state(state_file)
        bob_cursor_before = state["chats"]["bob@s.whatsapp.net"]

        # Now run favorites-only with just Alice
        out2 = tmp_path / "2.json"
        state2 = load_sync_state(state_file)
        s2 = extract_messages(
            db_path=synthetic_db,
            extracted_root=extracted_root,
            output_path=out2,
            attachments_dir=attachments_dir,
            sync_state=state2,
            mode="incremental",
            favorite_jids=["alice@s.whatsapp.net"],
        )
        if s2 is not None:
            _persist_cursors_like_push(state_file, s2)

        # Bob's cursor must remain untouched
        state2 = load_sync_state(state_file)
        assert state2["chats"].get("bob@s.whatsapp.net") == bob_cursor_before
