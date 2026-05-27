"""Tests for the ``sources/`` package + multi-source extraction.

Each source exposes a small, stable surface (``is_available``,
``db_path``, ``media_root``, ``snapshot``). These tests pin that
surface so a future refactor that breaks it gets caught early — and
covers the SQLite ``immutable=1`` URI behaviour for the Mac live
source, which is the load-bearing safety mechanism when reading from
a DB another process is writing to.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import (  # noqa: E402
    IphoneBackupSource,
    MacLiveSource,
    SourceNotAvailable,
    available_sources,
    get_source,
)


# ─── iPhone backup source ─────────────────────────────────────────────────


class TestIphoneBackupSource:
    def test_is_available_false_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("MIKOSHI_BACKUP_DIR", raising=False)
        s = IphoneBackupSource()
        assert s.is_available() is False

    def test_is_available_false_when_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(tmp_path))
        s = IphoneBackupSource()
        assert s.is_available() is False

    def test_paths_resolve_under_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(tmp_path))
        s = IphoneBackupSource()
        assert s.db_path() == tmp_path / "extracted" / "ChatStorage.sqlite"
        assert s.media_root() == tmp_path / "extracted"

    def test_explicit_backup_dir_overrides_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", "/should-be-ignored")
        s = IphoneBackupSource(backup_dir=tmp_path)
        assert s.db_path() == tmp_path / "extracted" / "ChatStorage.sqlite"


# ─── Mac live source ──────────────────────────────────────────────────────


def _make_minimal_chat_db(path: Path) -> None:
    """Build a no-frills ChatStorage with one chat, two messages, one
    media item. Used to exercise the Mac live source's snapshot()
    without needing the real group container."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT
        );
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZSTANZAID TEXT,
            ZMESSAGEDATE REAL
        );
        CREATE TABLE ZWAMEDIAITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZMESSAGE INTEGER,
            ZMEDIALOCALPATH TEXT
        );
        INSERT INTO ZWACHATSESSION VALUES (1, 'alice@s.whatsapp.net');
        INSERT INTO ZWAMESSAGE VALUES (1, 1, 'S1', 100.0);
        INSERT INTO ZWAMESSAGE VALUES (2, 1, 'S2', 200.0);
        INSERT INTO ZWAMEDIAITEM VALUES (10, 2, 'Media/photo.jpg');
        INSERT INTO ZWAMEDIAITEM VALUES (11, 2, NULL); -- thumbnail-only
        """
    )
    conn.commit()
    conn.close()


class TestMacLiveSource:
    def test_is_available_false_when_group_container_missing(self, tmp_path):
        s = MacLiveSource(root=tmp_path / "does-not-exist")
        assert s.is_available() is False

    def test_is_available_true_when_db_present(self, tmp_path):
        _make_minimal_chat_db(tmp_path / "ChatStorage.sqlite")
        s = MacLiveSource(root=tmp_path)
        assert s.is_available() is True

    def test_snapshot_reports_counts(self, tmp_path):
        _make_minimal_chat_db(tmp_path / "ChatStorage.sqlite")
        s = MacLiveSource(root=tmp_path)
        snap = s.snapshot()
        assert snap.name == "mac_live"
        assert snap.message_count == 2
        # One media item with ZMEDIALOCALPATH, one without.
        assert snap.media_with_local_path == 1
        assert snap.db_path == tmp_path / "ChatStorage.sqlite"

    def test_snapshot_raises_when_db_missing(self, tmp_path):
        s = MacLiveSource(root=tmp_path)
        with pytest.raises(SourceNotAvailable):
            s.snapshot()

    def test_readonly_uri_is_immutable(self, tmp_path):
        """The Mac live DB is being written to by another process while
        we read it. The ``mode=ro&immutable=1`` URI tells SQLite to
        skip locking and treat the file as a stable snapshot — without
        this we could see "database is locked" failures."""
        _make_minimal_chat_db(tmp_path / "ChatStorage.sqlite")
        s = MacLiveSource(root=tmp_path)
        uri = s._readonly_uri(s.db_path())
        assert "mode=ro" in uri
        assert "immutable=1" in uri
        # Confirm the connection actually opens with that URI.
        conn = sqlite3.connect(uri, uri=True)
        assert conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0] == 2
        conn.close()


# ─── registry ─────────────────────────────────────────────────────────────


class TestRegistry:
    def test_get_source_known(self):
        s = get_source("iphone_backup")
        assert isinstance(s, IphoneBackupSource)
        s = get_source("mac_live")
        assert isinstance(s, MacLiveSource)

    def test_get_source_unknown_raises(self):
        with pytest.raises(KeyError):
            get_source("nonexistent_source")

    def test_available_sources_does_not_crash(self):
        """``available_sources()`` must be safe to call regardless of
        whether the user has a backup or the Mac live DB. The exact
        return set depends on the dev's machine, so we just assert
        it's a list and every entry is actually available."""
        result = available_sources()
        assert isinstance(result, list)
        for s in result:
            assert s.is_available() is True


# ─── multi-source extraction ──────────────────────────────────────────────


def _build_full_synthetic_db(path: Path, *, rows: list[tuple]) -> None:
    """``rows`` items are (pk, chat_session_jid, stanza, message_date_iso, text)."""
    from datetime import datetime
    conn = sqlite3.connect(path)
    conn.executescript(
        """
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
        INSERT INTO ZWACHATSESSION VALUES (1, 'alice@s.whatsapp.net', 'Alice', NULL);
        """
    )
    for pk, _jid, stanza, ts_iso, text in rows:
        # Convert ISO timestamp → iOS Core Data seconds.
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        ios_ts = dt.timestamp() - 978307200.0
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (?, 1, ?, ?, 'alice@s.whatsapp.net', 'me@s.whatsapp.net', 0, 0, 'Alice', ?)",
            (pk, text, ios_ts, stanza),
        )
    conn.commit()
    conn.close()


class TestMultiSourceExtraction:
    def test_union_with_overlap_dedups(self, tmp_path):
        """Two sources both have S1; only one row makes it into the
        manifest. S-iphone-only and S-mac-only pass through."""
        from extract_messages import extract_messages_multi_source, load_sync_state

        ip_root = tmp_path / "iphone"
        mc_root = tmp_path / "mac"
        (ip_root / "extracted").mkdir(parents=True)
        mc_root.mkdir()

        _build_full_synthetic_db(
            ip_root / "extracted" / "ChatStorage.sqlite",
            rows=[
                (1, "alice@s.whatsapp.net", "S1", "2026-05-25T10:00:00Z", "shared"),
                (2, "alice@s.whatsapp.net", "S-iphone-only", "2026-05-25T11:00:00Z", "iphone only"),
            ],
        )
        _build_full_synthetic_db(
            mc_root / "ChatStorage.sqlite",
            rows=[
                (1, "alice@s.whatsapp.net", "S1", "2026-05-25T10:00:00Z", "shared"),
                (2, "alice@s.whatsapp.net", "S-mac-only", "2026-05-26T11:00:00Z", "mac only"),
            ],
        )

        ip = IphoneBackupSource(backup_dir=ip_root)
        mc = MacLiveSource(root=mc_root)

        out = tmp_path / "manifest.json"
        state = load_sync_state(tmp_path / ".sync_state.json")
        result = extract_messages_multi_source(
            sources=[ip, mc],
            output_path=out,
            attachments_dir=tmp_path / "attachments",
            sync_state=state,
            mode="full",
        )

        manifest = json.loads(out.read_text())
        # 3 unique messages: S1 (dedup), S-iphone-only, S-mac-only.
        assert manifest["stats"]["total_messages"] == 3
        assert manifest["stats"]["total_chats"] == 1
        # Sources field captures which sources fed the manifest.
        assert set(manifest["sources"]) == {"iphone_backup", "mac_live"}

        chat = manifest["chats"][0]
        ext_ids = {m["external_id"] for m in chat["messages"]}
        assert ext_ids == {"wa:S1", "wa:S-iphone-only", "wa:S-mac-only"}

    def test_attachment_provenance_prefers_iphone(self, tmp_path):
        """A real-world scenario the Mac live DB hits constantly: it
        has the message metadata for an image but no bytes on disk.
        The iPhone backup has both. Reconciler should keep the iPhone's
        attachment in the merged manifest."""
        from extract_messages import extract_messages_multi_source, load_sync_state

        ip_root = tmp_path / "iphone"
        mc_root = tmp_path / "mac"
        (ip_root / "extracted").mkdir(parents=True)
        mc_root.mkdir()

        _build_full_synthetic_db(
            ip_root / "extracted" / "ChatStorage.sqlite",
            rows=[(1, "alice@s.whatsapp.net", "S-MEDIA", "2026-05-25T10:00:00Z", None)],
        )
        # Wire up an actual attachment on the iPhone side: ZWAMEDIAITEM
        # row pointing to a real file under extracted/.
        conn = sqlite3.connect(ip_root / "extracted" / "ChatStorage.sqlite")
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (1, 1, 'Media/img.jpg', 'image/jpeg', 'img.jpg', 1024)"
        )
        conn.commit()
        conn.close()
        media_path = ip_root / "extracted" / "Media" / "img.jpg"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"PHOTOBYTES")

        # Mac side: same stanza, but no media bytes on disk.
        _build_full_synthetic_db(
            mc_root / "ChatStorage.sqlite",
            rows=[(1, "alice@s.whatsapp.net", "S-MEDIA", "2026-05-25T10:00:00Z", None)],
        )
        conn = sqlite3.connect(mc_root / "ChatStorage.sqlite")
        # Media metadata exists but ZMEDIALOCALPATH is NULL (cloud-fetch only).
        conn.execute("INSERT INTO ZWAMEDIAITEM VALUES (1, 1, NULL, 'image/jpeg', 'img.jpg', 1024)")
        conn.commit()
        conn.close()

        ip = IphoneBackupSource(backup_dir=ip_root)
        mc = MacLiveSource(root=mc_root)

        out = tmp_path / "manifest.json"
        state = load_sync_state(tmp_path / ".sync_state.json")
        extract_messages_multi_source(
            sources=[ip, mc],
            output_path=out,
            attachments_dir=tmp_path / "attachments",
            sync_state=state,
            mode="full",
        )

        manifest = json.loads(out.read_text())
        assert manifest["stats"]["total_messages"] == 1
        msg = manifest["chats"][0]["messages"][0]
        att = msg["attachment"]
        # The kept attachment is the iPhone's — has sha256 and skipped=False.
        assert att is not None
        assert att["skipped"] is False
        assert "sha256" in att
