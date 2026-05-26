"""Tests for non-interactive helpers in tui.py.

Only covers logic (config loading, env propagation, db location, formatting).
The Rich/questionary UI flow isn't tested here — it's interactive.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── load_ingest_conf ──────────────────────────────────────────────────────

class TestLoadIngestConf:
    def _isolated_tui(self, monkeypatch, conf_path):
        """Reload tui.py with a swapped INGEST_CONF pointing at conf_path."""
        # Clear from cache to force re-import with new env
        for mod in ("tui",):
            sys.modules.pop(mod, None)
        monkeypatch.setenv("MIKOSHI_INGEST_CONF", str(conf_path))
        # Strip out any pre-existing config-derived vars. The list MUST stay
        # in sync with tui.INGEST_CONF_KEYS — otherwise a stale env value
        # from an earlier test leaks into load_ingest_conf's output and
        # breaks the "empty conf → empty cfg" expectation.
        for k in ("MIKOSHI_URL", "MIKOSHI_TOKEN", "MIKOSHI_BACKUP_DIR",
                  "MIKOSHI_CLIENT_ID", "KEEP_LOCAL_EXPORTS",
                  "MIKOSHI_FAVORITES_FILE", "MIKOSHI_PRESERVE_EXTRACTED"):
            monkeypatch.delenv(k, raising=False)
        import tui  # re-import
        return tui

    def test_parses_key_value(self, tmp_path, monkeypatch):
        conf = tmp_path / "ingest.conf"
        conf.write_text(
            "MIKOSHI_URL=https://example.com\n"
            "MIKOSHI_TOKEN=tok-123\n"
            "MIKOSHI_BACKUP_DIR=/Volumes/ExternalSSD/backup\n"
        )
        tui = self._isolated_tui(monkeypatch, conf)
        cfg = tui.load_ingest_conf()
        assert cfg["MIKOSHI_URL"] == "https://example.com"
        assert cfg["MIKOSHI_TOKEN"] == "tok-123"
        assert cfg["MIKOSHI_BACKUP_DIR"] == "/Volumes/ExternalSSD/backup"

    def test_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        conf = tmp_path / "ingest.conf"
        conf.write_text(
            "# a comment\n"
            "\n"
            "MIKOSHI_URL=https://example.com\n"
            "  # indented comment\n"
        )
        tui = self._isolated_tui(monkeypatch, conf)
        cfg = tui.load_ingest_conf()
        assert cfg["MIKOSHI_URL"] == "https://example.com"

    def test_strips_quotes(self, tmp_path, monkeypatch):
        conf = tmp_path / "ingest.conf"
        conf.write_text('MIKOSHI_URL="https://quoted.example.com"\n')
        tui = self._isolated_tui(monkeypatch, conf)
        cfg = tui.load_ingest_conf()
        assert cfg["MIKOSHI_URL"] == "https://quoted.example.com"

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        conf = tmp_path / "ingest.conf"
        conf.write_text("MIKOSHI_URL=from-file\n")
        monkeypatch.setenv("MIKOSHI_URL", "from-env")
        # _isolated_tui clears env, so set it again after
        for k in ("MIKOSHI_TOKEN", "MIKOSHI_BACKUP_DIR", "MIKOSHI_CLIENT_ID",
                  "KEEP_LOCAL_EXPORTS", "MIKOSHI_FAVORITES_FILE"):
            monkeypatch.delenv(k, raising=False)
        sys.modules.pop("tui", None)
        monkeypatch.setenv("MIKOSHI_INGEST_CONF", str(conf))
        monkeypatch.setenv("MIKOSHI_URL", "from-env")
        import tui
        cfg = tui.load_ingest_conf()
        assert cfg["MIKOSHI_URL"] == "from-env"

    def test_exports_to_environ_for_subprocess(self, tmp_path, monkeypatch):
        """File-loaded vars must end up in os.environ so subprocs inherit."""
        conf = tmp_path / "ingest.conf"
        conf.write_text(
            "MIKOSHI_BACKUP_DIR=/Volumes/test/backup\n"
            "MIKOSHI_URL=https://test.example.com\n"
        )
        tui = self._isolated_tui(monkeypatch, conf)
        tui.load_ingest_conf()
        assert os.environ.get("MIKOSHI_BACKUP_DIR") == "/Volumes/test/backup"
        assert os.environ.get("MIKOSHI_URL") == "https://test.example.com"

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        tui = self._isolated_tui(monkeypatch, tmp_path / "nonexistent.conf")
        cfg = tui.load_ingest_conf()
        assert cfg == {}


# ─── fmt_ts ────────────────────────────────────────────────────────────────

class TestBestFromPhase:
    """The 'Sync — one contact only' bug: pipeline started at Phase 1
    (Device Detection) and failed when the iPhone wasn't connected, even
    though a perfectly good backup was on disk. _best_from_phase picks
    the cheapest --from-phase based on what's already decrypted/encrypted.
    """

    def _reload_tui(self, monkeypatch, backup_dir):
        sys.modules.pop("tui", None)
        if backup_dir is None:
            monkeypatch.delenv("MIKOSHI_BACKUP_DIR", raising=False)
        else:
            monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(backup_dir))
        # Strip any inherited conf path that could shadow our env
        monkeypatch.setenv("MIKOSHI_INGEST_CONF", "/tmp/__test_no_conf__")
        import tui
        return tui

    def test_no_backup_at_all_returns_phase_1(self, tmp_path, monkeypatch):
        tui = self._reload_tui(monkeypatch, None)
        phase, label = tui._best_from_phase()
        assert phase == 1
        # Label must surface that Phase 1 is incremental (regression: it used
        # to say "full sync" which scared users away from the right choice)
        assert "iPhone" in label
        assert "incremental" in label.lower()

    def test_encrypted_only_returns_phase_3(self, tmp_path, monkeypatch):
        # Set up a fake encrypted backup
        udid_dir = tmp_path / "backup" / "00008130-00011234567890ABCDEF"
        udid_dir.mkdir(parents=True)
        (udid_dir / "Manifest.plist").write_bytes(b"bplist00" + b"\x00" * 1000)

        tui = self._reload_tui(monkeypatch, tmp_path)
        phase, label = tui._best_from_phase()
        assert phase == 3
        assert "no iphone" in label.lower()
        assert "re-decrypt" in label.lower()

    def test_decrypted_returns_phase_4(self, tmp_path, monkeypatch):
        # Both encrypted backup and decrypted ChatStorage available
        udid_dir = tmp_path / "backup" / "00008130-00011234567890ABCDEF"
        udid_dir.mkdir(parents=True)
        (udid_dir / "Manifest.plist").write_bytes(b"bplist00" + b"\x00" * 1000)
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "ChatStorage.sqlite").write_bytes(b"SQLite format 3\x00")

        tui = self._reload_tui(monkeypatch, tmp_path)
        phase, label = tui._best_from_phase()
        assert phase == 4
        assert "extract-only" in label.lower()

    def test_empty_manifest_doesnt_count_as_encrypted(self, tmp_path, monkeypatch):
        # Zero-byte Manifest.plist means the backup is corrupt → don't claim phase 3
        udid_dir = tmp_path / "backup" / "00008130-00011234567890ABCDEF"
        udid_dir.mkdir(parents=True)
        (udid_dir / "Manifest.plist").write_bytes(b"")
        tui = self._reload_tui(monkeypatch, tmp_path)
        phase, _ = tui._best_from_phase()
        assert phase == 1

    def test_corrupt_chatstorage_falls_back_to_phase_3(self, tmp_path, monkeypatch):
        """Regression: a 1.1 GB ChatStorage.sqlite full of zeros (from a
        killed Phase 3 mid-write) used to pass `size > 0` and crash
        Phase 4 with 'file is not a database'. _best_from_phase must
        validate the SQLite header and fall back to re-decrypt."""
        udid_dir = tmp_path / "backup" / "00008130-00011234567890ABCDEF"
        udid_dir.mkdir(parents=True)
        (udid_dir / "Manifest.plist").write_bytes(b"bplist00" + b"\x00" * 1000)
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        # Looks fine to the eye (large, non-empty) but invalid header
        (extracted / "ChatStorage.sqlite").write_bytes(b"\x00" * 4096)

        tui = self._reload_tui(monkeypatch, tmp_path)
        phase, label = tui._best_from_phase()
        assert phase == 3, "Corrupt SQLite must not be trusted; fall back to Phase 3"
        assert "re-decrypt" in label.lower()


class TestFmtTs:
    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_none(self):
        assert self.tui.fmt_ts(None) == "—"

    def test_zero(self):
        assert self.tui.fmt_ts(0) == "—"  # falsy

    def test_real_value(self):
        # 2026-05-25 12:00 UTC → iOS epoch = 2026-05-25 - 2001-01-01
        ios_ts = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc).timestamp() - 978307200
        result = self.tui.fmt_ts(ios_ts)
        assert result == "2026-05-25"

    def test_absurd_value_returns_placeholder(self):
        # Real ChatStorage.sqlite files occasionally carry junk timestamps
        # (year 11001 etc.) on rows from uninitialised system events.
        # fmt_ts must not crash the whole "List chats" view.
        assert self.tui.fmt_ts(9.99e11) == "—"
        assert self.tui.fmt_ts(-1e12) == "—"

    def test_garbage_input_returns_placeholder(self):
        # Defensive: even if a row somehow contains a non-numeric / NaN
        # value, the listing should degrade gracefully.
        assert self.tui.fmt_ts(float("nan")) == "—"
        assert self.tui.fmt_ts(float("inf")) == "—"


# ─── find_existing_chatstorage ─────────────────────────────────────────────

class TestFindExistingChatstorage:
    def test_finds_in_temp_extracted(self, tmp_path, monkeypatch):
        sys.modules.pop("tui", None)
        import tui
        # Point SCRIPT_DIR (and therefore the implicit temp/extracted path) at tmp
        monkeypatch.setattr(tui, "SCRIPT_DIR", tmp_path)
        # No backup dir set → should look at SCRIPT_DIR/temp/extracted only
        monkeypatch.delenv("MIKOSHI_BACKUP_DIR", raising=False)

        extracted = tmp_path / "temp" / "extracted"
        extracted.mkdir(parents=True)
        db = extracted / "ChatStorage.sqlite"
        db.write_bytes(b"x")

        # Reload load_ingest_conf to refresh internal state
        tui.load_ingest_conf()
        result = tui.find_existing_chatstorage()
        assert result == db

    def test_finds_in_external_backup_dir(self, tmp_path, monkeypatch):
        sys.modules.pop("tui", None)
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(tmp_path / "backup"))
        import tui
        monkeypatch.setattr(tui, "SCRIPT_DIR", tmp_path / "script")
        (tmp_path / "script").mkdir()

        ext_extracted = tmp_path / "backup" / "extracted"
        ext_extracted.mkdir(parents=True)
        db = ext_extracted / "ChatStorage.sqlite"
        db.write_bytes(b"x")

        result = tui.find_existing_chatstorage()
        assert result == db

    def test_returns_none_when_missing(self, tmp_path, monkeypatch):
        sys.modules.pop("tui", None)
        monkeypatch.delenv("MIKOSHI_BACKUP_DIR", raising=False)
        # Point INGEST_CONF at a nonexistent path so the developer's real
        # ~/.mikoshi-ingest.conf doesn't leak MIKOSHI_BACKUP_DIR into the test.
        monkeypatch.setenv("MIKOSHI_INGEST_CONF", str(tmp_path / "nope.conf"))
        import tui
        monkeypatch.setattr(tui, "SCRIPT_DIR", tmp_path)
        assert tui.find_existing_chatstorage() is None


# ─── list_chats_from_db ────────────────────────────────────────────────────

class TestListChatsFromDb:
    def _make_db(self, path):
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
        """)
        conn.execute("INSERT INTO ZWACHATSESSION VALUES (1, 'a@s.whatsapp.net', 'A', 100.0)")
        conn.execute("INSERT INTO ZWACHATSESSION VALUES (2, 'b@s.whatsapp.net', 'B', 200.0)")
        conn.execute("INSERT INTO ZWAMESSAGE VALUES (1, 1)")
        conn.execute("INSERT INTO ZWAMESSAGE VALUES (2, 1)")
        conn.execute("INSERT INTO ZWAMESSAGE VALUES (3, 2)")
        conn.commit()
        conn.close()

    def test_returns_chats_with_counts(self, tmp_path):
        sys.modules.pop("tui", None)
        import tui
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        rows = tui.list_chats_from_db(db)
        assert len(rows) == 2
        by_jid = {r["jid"]: r for r in rows}
        assert by_jid["a@s.whatsapp.net"]["msg_count"] == 2
        assert by_jid["b@s.whatsapp.net"]["msg_count"] == 1

    def test_ordered_by_recency(self, tmp_path):
        sys.modules.pop("tui", None)
        import tui
        db = tmp_path / "db.sqlite"
        self._make_db(db)
        rows = tui.list_chats_from_db(db)
        # B has higher last_ts → first
        assert rows[0]["jid"] == "b@s.whatsapp.net"


# ─── _dir_size_gb (du wrapper) ─────────────────────────────────────────────

class TestDirSizeGb:
    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_small_dir(self, tmp_path):
        (tmp_path / "a").write_bytes(b"x" * 1024)
        result = self.tui._dir_size_gb(tmp_path)
        # Should be ≪ 1 GB, format "X.X MB"
        assert "MB" in result or "GB" in result

    def test_nonexistent_path_no_crash(self, tmp_path):
        result = self.tui._dir_size_gb(tmp_path / "nope")
        # du errors out → friendly fallback, not exception
        assert isinstance(result, str)

    def test_timeout_returns_placeholder(self, tmp_path, monkeypatch):
        import subprocess

        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="du", timeout=0.01)

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = self.tui._dir_size_gb(tmp_path, timeout=0.01)
        assert "computing" in result or "large" in result


# ─── ACTIONS / main loop invariants ────────────────────────────────────────

class TestActionsTable:
    """Guard against the 'str object is not callable' regression in main()."""

    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_all_actions_are_callable(self):
        for label, fn in self.tui.ACTIONS:
            assert callable(fn), f"Action {label!r} has non-callable target: {fn!r}"

    def test_all_actions_have_unique_labels(self):
        labels = [label for label, _ in self.tui.ACTIONS]
        assert len(labels) == len(set(labels)), "Duplicate action labels"

    def test_exit_sentinel_distinct_from_None(self):
        """If Exit's sentinel collides with None, ESC handling breaks."""
        assert self.tui._EXIT_SENTINEL is not None
        # And the sentinel itself must not be callable (so main() won't try to call it)
        assert not callable(self.tui._EXIT_SENTINEL)

    def test_at_least_basic_actions_present(self):
        """Smoke check: the menu has the entries we documented."""
        labels = [label for label, _ in self.tui.ACTIONS]
        # Fuzzy match: each expected keyword shows up in at least one label
        for keyword in ("status", "Verify", "List chats", "Manage favorites",
                        "Sync", "Push", "sqlite"):
            assert any(keyword.lower() in lbl.lower() for lbl in labels), \
                f"Missing menu entry containing {keyword!r}"
