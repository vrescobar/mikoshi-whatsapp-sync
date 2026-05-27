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
        db.write_bytes(b"SQLite format 3\x00")  # validation requires real header

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
        db.write_bytes(b"SQLite format 3\x00")  # validation requires real header

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
    """Guard against the 'str object is not callable' regression in main()
    and verify the intent-based menu still exposes the screens users
    expect to find via keyword search.
    """

    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_all_actions_dispatch_to_callables(self):
        """Each entry in ACTIONS is (label, dispatch_key); the dispatch
        table maps every key to a real function."""
        for label, key in self.tui.ACTIONS:
            assert key in self.tui._ACTION_DISPATCH, \
                f"Action {label!r} maps to key {key!r} which has no dispatch entry"
            assert callable(self.tui._ACTION_DISPATCH[key]), \
                f"Dispatch entry for {key!r} is not callable"

    def test_all_actions_have_unique_labels(self):
        labels = [label for label, _ in self.tui.ACTIONS]
        assert len(labels) == len(set(labels)), "Duplicate action labels"

    def test_exit_sentinel_distinct_from_None(self):
        assert self.tui._EXIT_SENTINEL is not None
        assert not callable(self.tui._EXIT_SENTINEL)

    def test_intent_keywords_discoverable(self):
        """The 5-screen menu is built around intent. Each pre-redesign
        keyword still appears somewhere in the labels so users hunting
        for 'Push' or 'sqlite' via questionary's filter find a route."""
        labels = [label for label, _ in self.tui.ACTIONS]
        for keyword in ("status", "Verify", "List chats", "Manage favorites",
                        "Sync", "Push", "sqlite"):
            assert any(keyword.lower() in lbl.lower() for lbl in labels), \
                f"Missing menu entry containing {keyword!r}"


# ─── multi-source: probe / picker / label helpers ──────────────────────────

class TestProbeSources:
    """``_probe_sources`` must never raise — the TUI header refresh path
    calls it on every render."""

    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_returns_one_entry_per_registered_source(self):
        result = self.tui._probe_sources()
        # Exactly two sources registered today: iphone_backup + mac_live.
        names = {e["name"] for e in result}
        assert names == {"iphone_backup", "mac_live"}, \
            f"unexpected source registry: {names}"

    def test_each_entry_has_required_keys(self):
        for entry in self.tui._probe_sources():
            assert set(entry.keys()) >= {"name", "available", "snapshot", "error"}

    def test_unavailable_source_reports_cleanly(self, monkeypatch, tmp_path):
        # Force both sources to look unavailable — point MIKOSHI_BACKUP_DIR at
        # a nonexistent path and override the mac_live root.
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(tmp_path / "no-backup"))
        from sources import mac_live
        monkeypatch.setattr(mac_live, "GROUP_CONTAINER", tmp_path / "no-mac")
        result = self.tui._probe_sources()
        for entry in result:
            # When the file isn't there, is_available() returns False and we
            # don't try to snapshot — so error stays None and snapshot is None.
            assert entry["available"] is False
            assert entry["snapshot"] is None

    def test_swallowed_snapshot_exception_lands_in_error(self, monkeypatch):
        """If is_available() lies and snapshot() blows up, the probe must
        capture the error string rather than crashing the header."""
        from sources import IphoneBackupSource

        def fake_available(self):
            return True

        def fake_snapshot(self):
            raise RuntimeError("simulated lock contention")

        monkeypatch.setattr(IphoneBackupSource, "is_available", fake_available)
        monkeypatch.setattr(IphoneBackupSource, "snapshot", fake_snapshot)

        result = self.tui._probe_sources()
        by_name = {e["name"]: e for e in result}
        iphone = by_name["iphone_backup"]
        assert iphone["available"] is True
        assert iphone["snapshot"] is None
        assert "simulated lock contention" in iphone["error"]


class TestSourcesSummaryRow:
    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def _entry(self, name, available=True, count=0, mtime="2026-05-28T18:42:00+00:00"):
        from sources.base import SourceSnapshot
        if not available:
            return {"name": name, "available": False, "snapshot": None, "error": None}
        snap = SourceSnapshot(
            name=name,
            db_path=Path("/tmp/fake.sqlite"),
            mtime_iso=mtime,
            message_count=count,
            media_with_local_path=0,
        )
        return {"name": name, "available": True, "snapshot": snap, "error": None}

    def test_empty(self):
        assert "none" in self.tui._sources_summary_row([]).lower()

    def test_both_available_shows_counts(self):
        row = self.tui._sources_summary_row([
            self._entry("iphone_backup", count=1_032_268),
            self._entry("mac_live", count=303_642),
        ])
        # Both labels present
        assert "iPhone bkp" in row
        assert "Mac live" in row
        # Compact counts (1.0M, 304k)
        assert "1.0M" in row
        assert "304k" in row
        # Time formatted as HH:MM
        assert "18:42" in row

    def test_one_available_one_missing(self):
        row = self.tui._sources_summary_row([
            self._entry("iphone_backup", count=500_000),
            self._entry("mac_live", available=False),
        ])
        assert "iPhone bkp" in row
        # Missing source gets a dim "not linked" hint
        assert "not linked" in row

    def test_small_count_not_rounded_to_zero_k(self):
        row = self.tui._sources_summary_row([
            self._entry("iphone_backup", count=42),
        ])
        assert "42" in row
        assert "k" not in row.replace("bkp", "")  # no spurious "k" suffix


class TestPickSourcesNonInteractive:
    """``_pick_sources`` short-circuits when there's nothing to ask. The
    interactive 2-source branch is exercised by the action_sync wiring
    but not unit-tested here (it requires a TTY)."""

    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_no_sources_falls_back_to_iphone(self):
        result = self.tui._pick_sources([])
        assert result == ["iphone_backup"]

    def test_single_available_source_returns_it(self):
        entries = [
            {"name": "iphone_backup", "available": False, "snapshot": None, "error": None},
            {"name": "mac_live", "available": True, "snapshot": None, "error": None},
        ]
        assert self.tui._pick_sources(entries) == ["mac_live"]

    def test_only_iphone_available_returns_it(self):
        entries = [
            {"name": "iphone_backup", "available": True, "snapshot": None, "error": None},
            {"name": "mac_live", "available": False, "snapshot": None, "error": None},
        ]
        assert self.tui._pick_sources(entries) == ["iphone_backup"]


class TestFormatSourcesLabel:
    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def _entry(self, name, count):
        from sources.base import SourceSnapshot
        return {
            "name": name,
            "available": True,
            "snapshot": SourceSnapshot(
                name=name,
                db_path=Path("/tmp/x.sqlite"),
                mtime_iso="2026-05-28T18:42:00+00:00",
                message_count=count,
                media_with_local_path=0,
            ),
            "error": None,
        }

    def test_single_source_no_reconcile_suffix(self):
        label = self.tui._format_sources_label(
            ["iphone_backup"], [self._entry("iphone_backup", 100_000)]
        )
        assert "iPhone backup" in label
        assert "reconciled" not in label

    def test_two_sources_adds_reconcile_suffix(self):
        entries = [
            self._entry("iphone_backup", 1_032_268),
            self._entry("mac_live", 303_642),
        ]
        label = self.tui._format_sources_label(["iphone_backup", "mac_live"], entries)
        assert "iPhone backup" in label
        assert "Mac live" in label
        assert "reconciled" in label
        assert "+" in label  # joiner


class TestRenderPlanMultiSource:
    """When ``ChatPlanEntry.per_source`` is populated, the per-chat
    table grows extra columns. Renders to an in-memory Console so we
    can string-match the output without touching the real terminal.
    """

    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def _make_plan(self, *, multi: bool):
        from pipeline_state import Plan, ChatPlanEntry
        entry = ChatPlanEntry(
            jid="alice@s.whatsapp.net",
            name="Alice",
            cutoff_ts="2026-05-24T00:00:00+00:00",
            new_messages=7,
            new_attachments=2,
            per_source=(
                {
                    "iphone_backup": {"new_messages": 3, "max_ts": None},
                    "mac_live": {"new_messages": 7, "max_ts": None},
                }
                if multi else None
            ),
        )
        return Plan(scope="all", chats=[entry], server_endpoint_present=True)

    def _capture(self, plan):
        from rich.console import Console
        from io import StringIO
        buf = StringIO()
        cap_console = Console(file=buf, force_terminal=False, width=140, no_color=True)
        # Monkey-patch the module-level console to capture
        original = self.tui.console
        self.tui.console = cap_console
        try:
            self.tui.render_plan(
                plan, scope_label="all", source_label="Mac live only",
                sources_label="iPhone backup (X) + Mac live (Y)",
            )
        finally:
            self.tui.console = original
        return buf.getvalue()

    def test_single_source_keeps_classic_columns(self):
        out = self._capture(self._make_plan(multi=False))
        assert "New msgs" in out
        assert "iPhone +" not in out
        assert "Mac +" not in out
        assert "Unique" not in out

    def test_multi_source_shows_per_source_columns(self):
        out = self._capture(self._make_plan(multi=True))
        # New header columns
        assert "iPhone +" in out
        assert "Mac +" in out
        assert "Unique" in out
        # Per-source counts present as cells
        assert "3" in out  # iPhone count
        assert "7" in out  # Mac count + Unique≈


class TestExtraDbsForSources:
    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_returns_none_for_iphone_only(self, monkeypatch):
        # iphone_backup is the primary; nothing goes into extra_dbs
        from sources import IphoneBackupSource
        monkeypatch.setattr(IphoneBackupSource, "is_available", lambda self: True)
        assert self.tui._extra_dbs_for_sources(["iphone_backup"]) is None

    def test_includes_mac_when_selected_and_available(self, monkeypatch, tmp_path):
        from sources import MacLiveSource
        fake_db = tmp_path / "ChatStorage.sqlite"
        fake_db.write_bytes(b"SQLite format 3\x00")
        monkeypatch.setattr(MacLiveSource, "is_available", lambda self: True)
        monkeypatch.setattr(MacLiveSource, "db_path", lambda self: fake_db)
        extras = self.tui._extra_dbs_for_sources(["iphone_backup", "mac_live"])
        assert extras == {"mac_live": fake_db}

    def test_skips_unavailable_source(self, monkeypatch):
        from sources import MacLiveSource
        monkeypatch.setattr(MacLiveSource, "is_available", lambda self: False)
        assert self.tui._extra_dbs_for_sources(["iphone_backup", "mac_live"]) is None


class TestFavoritesRemoveChoices:
    """The favorites-remove picker used to iterate file-insertion order.
    Users with months of favorites would scroll past stale chats to find
    the one they actually wanted to remove. Newest-last-message-first
    matches every other picker in the app.
    """

    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_orders_by_last_ts_desc(self, monkeypatch, tmp_path):
        db = tmp_path / "ChatStorage.sqlite"
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
                ZCHATSESSION INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'old@s.whatsapp.net', 'Old', 100.0);
            INSERT INTO ZWACHATSESSION VALUES (2, 'mid@s.whatsapp.net', 'Mid', 500.0);
            INSERT INTO ZWACHATSESSION VALUES (3, 'new@s.whatsapp.net', 'New', 900.0);
        """)
        conn.commit()
        conn.close()

        monkeypatch.setattr(self.tui, "find_existing_chatstorage", lambda: db)

        # Insertion order is mixed
        favs = [
            {"jid": "mid@s.whatsapp.net", "name": "Mid"},
            {"jid": "old@s.whatsapp.net", "name": "Old"},
            {"jid": "new@s.whatsapp.net", "name": "New"},
        ]
        choices = self.tui._favorites_remove_choices(favs)
        # questionary.Choice.value carries the JID
        ordered = [c.value for c in choices]
        assert ordered == [
            "new@s.whatsapp.net",
            "mid@s.whatsapp.net",
            "old@s.whatsapp.net",
        ], f"Expected DESC by last_ts, got {ordered}"

    def test_missing_jid_sinks_to_bottom(self, monkeypatch, tmp_path):
        db = tmp_path / "ChatStorage.sqlite"
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
                ZCHATSESSION INTEGER
            );
            INSERT INTO ZWACHATSESSION VALUES (1, 'live@s.whatsapp.net', 'Live', 100.0);
        """)
        conn.commit()
        conn.close()

        monkeypatch.setattr(self.tui, "find_existing_chatstorage", lambda: db)

        favs = [
            {"jid": "deleted@s.whatsapp.net", "name": "Deleted"},
            {"jid": "live@s.whatsapp.net", "name": "Live"},
        ]
        choices = self.tui._favorites_remove_choices(favs)
        # Live first, deleted last; deleted label flagged
        assert choices[0].value == "live@s.whatsapp.net"
        assert choices[-1].value == "deleted@s.whatsapp.net"
        # The "no longer in local DB" marker is on the missing entry
        # questionary.Choice.title is the label (varies across versions)
        last_label = getattr(choices[-1], "title", None) or getattr(choices[-1], "label", None)
        # Some questionary versions expose the visible text via `.title`
        # as a list of (style, text) tuples. Coerce to string for matching.
        assert "no longer in local DB" in str(last_label)

    def test_no_db_falls_back_to_file_order(self, monkeypatch):
        # find_existing_chatstorage returns None → no sort, original order kept
        monkeypatch.setattr(self.tui, "find_existing_chatstorage", lambda: None)
        favs = [
            {"jid": "b@s.whatsapp.net", "name": "B"},
            {"jid": "a@s.whatsapp.net", "name": "A"},
        ]
        choices = self.tui._favorites_remove_choices(favs)
        assert [c.value for c in choices] == ["b@s.whatsapp.net", "a@s.whatsapp.net"]

    def test_empty_favorites(self, monkeypatch):
        monkeypatch.setattr(self.tui, "find_existing_chatstorage", lambda: None)
        assert self.tui._favorites_remove_choices([]) == []


class TestRunEnvExtra:
    """The MIKOSHI_SOURCES env var must reach the subprocess. Regression
    target: a refactor of ``run()`` would silently break multi-source
    sync because the bash wrapper only switches on this env var."""

    def setup_method(self):
        sys.modules.pop("tui", None)
        import tui
        self.tui = tui

    def test_env_extra_merged_into_subprocess_env(self, monkeypatch):
        captured = {}

        def fake_call(cmd, env=None, cwd=None):
            captured["env"] = env
            return 0

        monkeypatch.setattr("subprocess.call", fake_call)
        self.tui.run(["echo", "hi"], env_extra={"MIKOSHI_SOURCES": "iphone_backup,mac_live"})
        assert captured["env"]["MIKOSHI_SOURCES"] == "iphone_backup,mac_live"

    def test_no_env_extra_leaves_existing_env_untouched(self, monkeypatch):
        captured = {}

        def fake_call(cmd, env=None, cwd=None):
            captured["env"] = env
            return 0

        monkeypatch.setattr("subprocess.call", fake_call)
        monkeypatch.delenv("MIKOSHI_SOURCES", raising=False)
        self.tui.run(["echo", "hi"])
        assert "MIKOSHI_SOURCES" not in captured["env"]
