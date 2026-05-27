"""Tests for explore_backup.py — backup discovery + error paths."""

import os
import plistlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import explore_backup as eb


# ─── fixtures ──────────────────────────────────────────────────────────────

UDID = "00008130-0001184C1E46001C"


@pytest.fixture
def fake_backup_dir(tmp_path, monkeypatch):
    """Build a structurally-valid (but unreadable) backup tree."""
    base = tmp_path / "ExternalSSD" / "iphone_backup"
    udid = base / "backup" / UDID
    udid.mkdir(parents=True)
    # Valid plist content so plistlib.load() wouldn't crash on it
    (udid / "Manifest.plist").write_bytes(plistlib.dumps({"Version": "10"}))
    (udid / "Manifest.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4080)
    (udid / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "test"}))
    monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(base))
    return base


@pytest.fixture
def empty_backup_dir(tmp_path, monkeypatch):
    """Backup dir with the UDID folder but empty (simulates interrupted)."""
    base = tmp_path / "iphone_backup"
    udid = base / "backup" / UDID
    udid.mkdir(parents=True)
    monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(base))
    return base


@pytest.fixture
def truncated_backup_dir(tmp_path, monkeypatch):
    """UDID dir present but Manifest.plist is zero-length."""
    base = tmp_path / "iphone_backup"
    udid = base / "backup" / UDID
    udid.mkdir(parents=True)
    (udid / "Manifest.plist").write_bytes(b"")  # truncated
    (udid / "Manifest.db").write_bytes(b"SQLite format 3\x00")
    (udid / "Info.plist").write_bytes(plistlib.dumps({}))
    monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(base))
    return base


# ─── get_backup_dir ────────────────────────────────────────────────────────

class TestGetBackupDir:
    def test_env_set(self, monkeypatch):
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", "/some/path")
        assert eb.get_backup_dir() == Path("/some/path")

    def test_env_missing_exits(self, monkeypatch, capsys):
        monkeypatch.delenv("MIKOSHI_BACKUP_DIR", raising=False)
        with pytest.raises(SystemExit) as ei:
            eb.get_backup_dir()
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "MIKOSHI_BACKUP_DIR" in err


# ─── find_device_backup ────────────────────────────────────────────────────

class TestFindDeviceBackup:
    def test_finds_udid_dir(self, fake_backup_dir):
        result = eb.find_device_backup(fake_backup_dir)
        assert result.name == UDID

    def test_missing_backup_root_exits(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as ei:
            eb.find_device_backup(tmp_path / "nonexistent")
        assert ei.value.code == 1
        assert "No backup found" in capsys.readouterr().err

    def test_no_udid_dir_exits(self, tmp_path, capsys):
        (tmp_path / "backup").mkdir()
        with pytest.raises(SystemExit) as ei:
            eb.find_device_backup(tmp_path)
        assert ei.value.code == 1
        assert "No device backup" in capsys.readouterr().err

    def test_short_dirname_is_ignored(self, tmp_path, capsys):
        # Filenames < 20 chars are skipped (not a UDID)
        (tmp_path / "backup" / "short").mkdir(parents=True)
        with pytest.raises(SystemExit):
            eb.find_device_backup(tmp_path)


# ─── validate_backup_structure ─────────────────────────────────────────────

class TestValidateBackupStructure:
    def test_complete_backup_passes(self, fake_backup_dir):
        udid_dir = fake_backup_dir / "backup" / UDID
        eb.validate_backup_structure(udid_dir)  # no exception

    def test_missing_files_reported(self, empty_backup_dir, capsys):
        udid_dir = empty_backup_dir / "backup" / UDID
        with pytest.raises(SystemExit) as ei:
            eb.validate_backup_structure(udid_dir)
        assert ei.value.code == 3
        err = capsys.readouterr().err
        assert "Manifest.plist" in err
        assert "missing" in err
        # Actionable hint included
        assert "sync --all" in err

    def test_truncated_files_reported(self, truncated_backup_dir, capsys):
        udid_dir = truncated_backup_dir / "backup" / UDID
        with pytest.raises(SystemExit) as ei:
            eb.validate_backup_structure(udid_dir)
        assert ei.value.code == 3
        err = capsys.readouterr().err
        assert "empty" in err
        assert "Manifest.plist" in err


# ─── _safe_decrypt error mapping ───────────────────────────────────────────

class TestSafeDecrypt:
    def test_invalid_plist_maps_to_clear_error(self, capsys):
        def boom():
            raise plistlib.InvalidFileException()
        with pytest.raises(SystemExit) as ei:
            eb._safe_decrypt("test op", boom)
        assert ei.value.code == 3
        err = capsys.readouterr().err
        assert "corrupted" in err.lower() or "truncated" in err.lower()
        assert "sync --all" in err

    def test_password_error_maps_to_keychain_hint(self, capsys):
        def boom():
            raise ValueError("wrong passphrase or corrupted keybag")
        with pytest.raises(SystemExit) as ei:
            eb._safe_decrypt("test op", boom)
        assert ei.value.code == 3
        err = capsys.readouterr().err
        assert "password" in err.lower()
        assert "security delete-generic-password" in err

    def test_unrelated_value_error_reraises(self):
        def boom():
            raise ValueError("something unrelated")
        with pytest.raises(ValueError, match="something unrelated"):
            eb._safe_decrypt("test op", boom)

    def test_success_returns_value(self):
        assert eb._safe_decrypt("op", lambda: 42) == 42


# ─── decrypt_chatstorage skip-if-exists ────────────────────────────────────

class TestDecryptChatstorage:
    def test_skips_if_already_decrypted(self, tmp_path):
        """If ChatStorage.sqlite already exists with a valid SQLite header, no decrypt."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        existing = work_dir / "ChatStorage.sqlite"
        # Must look like a real SQLite file — the validation in decrypt_chatstorage
        # checks the magic header now (size>0 alone is no longer enough).
        existing.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

        # Patch decrypt machinery — if it gets called, the test fails
        with patch.object(eb, "EncryptedBackup", side_effect=AssertionError("should not decrypt")):
            result = eb.decrypt_chatstorage(work_dir)
        assert result == existing

    def test_re_decrypts_if_header_is_corrupt(self, fake_backup_dir, monkeypatch):
        """A 1.1 GB file of zeros (from a killed Phase 3) passes size>0 but
        isn't a real SQLite file. decrypt_chatstorage must reject and re-extract."""
        work_dir = fake_backup_dir / "extracted"
        work_dir.mkdir()
        # Looks fine to the eye, fails the header check
        corrupt = work_dir / "ChatStorage.sqlite"
        corrupt.write_bytes(b"\x00" * 4096)

        monkeypatch.setattr(eb, "get_passphrase", lambda: "fake-pw")
        fake_call = {"n": 0}

        class FakeBackup:
            def __init__(self, **kw): pass
            def extract_file(self, **kw):
                fake_call["n"] += 1
                Path(kw["output_filename"]).write_bytes(b"SQLite format 3\x00decrypted")

        monkeypatch.setattr(eb, "EncryptedBackup", FakeBackup)
        result = eb.decrypt_chatstorage(work_dir)
        assert fake_call["n"] == 1, "must re-decrypt when header is invalid"
        assert result.read_bytes().startswith(b"SQLite format 3\x00")

    def test_re_decrypts_if_zero_size(self, fake_backup_dir, monkeypatch):
        """A 0-byte ChatStorage from a previous failed run shouldn't be trusted."""
        work_dir = fake_backup_dir / "extracted"
        work_dir.mkdir()
        stale = work_dir / "ChatStorage.sqlite"
        stale.write_bytes(b"")

        # Stub out keychain + EncryptedBackup
        monkeypatch.setattr(eb, "get_passphrase", lambda: "fake-pw")
        fake_call = {"n": 0}

        class FakeBackup:
            def __init__(self, **kw): pass
            def extract_file(self, **kw):
                fake_call["n"] += 1
                Path(kw["output_filename"]).write_bytes(b"decrypted db")

        monkeypatch.setattr(eb, "EncryptedBackup", FakeBackup)
        result = eb.decrypt_chatstorage(work_dir)
        assert fake_call["n"] == 1
        assert result.read_bytes() == b"decrypted db"


# ─── decrypt_chatstorage validates structure ───────────────────────────────

class TestDecryptValidatesStructure:
    def test_decrypt_calls_validate(self, empty_backup_dir, monkeypatch, capsys):
        """decrypt_chatstorage must call validate_backup_structure first."""
        work_dir = empty_backup_dir / "extracted"
        monkeypatch.setattr(eb, "get_passphrase", lambda: "fake")
        with pytest.raises(SystemExit) as ei:
            eb.decrypt_chatstorage(work_dir)
        assert ei.value.code == 3  # from validate_backup_structure, not from elsewhere
        assert "Manifest.plist" in capsys.readouterr().err


# ─── REQUIRED_BACKUP_FILES constant ────────────────────────────────────────

class TestRequiredFiles:
    def test_includes_manifest_plist(self):
        assert "Manifest.plist" in eb.REQUIRED_BACKUP_FILES
        assert "Manifest.db" in eb.REQUIRED_BACKUP_FILES


# ─── list-chats survives garbage timestamps ───────────────────────────────

class TestListChatsTimestampSafety:
    """Regression: ZLASTMESSAGEDATE occasionally carries values that overflow
    datetime ('year must be in 1..9999, not 11001'). The whole listing
    must not crash because of one bad row.
    """

    def _build_db_with_bad_ts(self, path):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE ZWACHATSESSION (
                Z_PK INTEGER PRIMARY KEY,
                ZCONTACTJID TEXT,
                ZPARTNERNAME TEXT,
                ZLASTMESSAGEDATE REAL
            );
            CREATE TABLE ZWAMESSAGE (
                Z_PK INTEGER PRIMARY KEY,
                ZCHATSESSION INTEGER
            );
            -- A normal chat
            INSERT INTO ZWACHATSESSION VALUES
                (1, 'alice@s.whatsapp.net', 'Alice', 700000000.0);
            -- A row with an absurd timestamp (year 11001)
            INSERT INTO ZWACHATSESSION VALUES
                (2, 'ghost@s.whatsapp.net', 'Ghost', 285170400000.0);
            -- A row with a negative timestamp
            INSERT INTO ZWACHATSESSION VALUES
                (3, 'neg@s.whatsapp.net', 'Neg', -9999999999.0);
        """)
        conn.commit()
        conn.close()

    def test_cmd_list_chats_doesnt_crash_on_bad_ts(self, tmp_path, monkeypatch, capsys):
        from unittest.mock import patch

        db = tmp_path / "ChatStorage.sqlite"
        self._build_db_with_bad_ts(db)

        # cmd_list_chats calls decrypt_chatstorage which will try to decrypt;
        # short-circuit that by patching it to return the prebuilt DB.
        with patch.object(eb, "decrypt_chatstorage", return_value=db):
            from argparse import Namespace
            # Should NOT raise — print all 3 rows with "—" for the bad ones
            eb.cmd_list_chats(Namespace())

        out = capsys.readouterr().out
        # All three chats appeared
        assert "Alice" in out
        assert "Ghost" in out
        assert "Neg" in out
        # The bad-ts row's date got replaced with the placeholder
        assert "—" in out
        # And the good one still has a real date (700000000 iOS ts ≈ 2023)
        assert "2023-" in out
