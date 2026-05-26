"""Tests for verify_backup.py — 4 progressive integrity checks."""

import json
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import verify_backup as vb


UDID = "00008130-0001184C1E46001C"


# ─── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def good_backup(tmp_path):
    """A backup tree that passes levels 1 + 2 (structure + status)."""
    base = tmp_path / "iphone_backup"
    udid_dir = base / "backup" / UDID
    udid_dir.mkdir(parents=True)

    # Valid plist headers + non-zero content
    (udid_dir / "Manifest.plist").write_bytes(plistlib.dumps({"Version": "10.0"}))
    (udid_dir / "Status.plist").write_bytes(plistlib.dumps({
        "BackupState": "new",
        "SnapshotState": "finished",
        "IsFullBackup": True,
    }))
    (udid_dir / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "test"}))
    # Manifest.db with the SQLite magic
    (udid_dir / "Manifest.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 4080)

    return base, udid_dir


@pytest.fixture
def incomplete_backup(tmp_path):
    """Like good_backup but Status.plist says backup didn't finish."""
    base = tmp_path / "iphone_backup"
    udid_dir = base / "backup" / UDID
    udid_dir.mkdir(parents=True)
    (udid_dir / "Manifest.plist").write_bytes(plistlib.dumps({"Version": "10.0"}))
    (udid_dir / "Manifest.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    (udid_dir / "Status.plist").write_bytes(plistlib.dumps({
        "BackupState": "new",
        "SnapshotState": "uploading",  # not 'finished'
        "IsFullBackup": True,
    }))
    (udid_dir / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "test"}))
    return base, udid_dir


@pytest.fixture
def shredded_backup(tmp_path):
    """The exact failure mode we hit: files exist but are all NULs."""
    base = tmp_path / "iphone_backup"
    udid_dir = base / "backup" / UDID
    udid_dir.mkdir(parents=True)
    (udid_dir / "Manifest.plist").write_bytes(b"\x00" * 1024)
    (udid_dir / "Manifest.db").write_bytes(b"\x00" * 4096)
    (udid_dir / "Status.plist").write_bytes(b"\x00" * 512)
    (udid_dir / "Info.plist").write_bytes(b"\x00" * 512)
    return base, udid_dir


# ─── discover_backup_dir ───────────────────────────────────────────────────

class TestDiscovery:
    def test_finds_udid_via_env(self, good_backup, monkeypatch):
        base, udid_dir = good_backup
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(base))
        result = vb.discover_backup_dir()
        assert result == udid_dir

    def test_explicit_udid_dir(self, good_backup):
        base, udid_dir = good_backup
        assert vb.discover_backup_dir(udid_dir) == udid_dir

    def test_explicit_parent(self, good_backup):
        base, udid_dir = good_backup
        assert vb.discover_backup_dir(base) == udid_dir

    def test_no_env_no_arg_exits(self, monkeypatch):
        monkeypatch.delenv("MIKOSHI_BACKUP_DIR", raising=False)
        with pytest.raises(SystemExit):
            vb.discover_backup_dir()

    def test_missing_backup_dir_exits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MIKOSHI_BACKUP_DIR", str(tmp_path / "nope"))
        with pytest.raises(SystemExit):
            vb.discover_backup_dir()


# ─── Level 1: structure ────────────────────────────────────────────────────

class TestCheckStructure:
    def test_passes_on_good_backup(self, good_backup):
        _, udid_dir = good_backup
        r = vb.check_structure(udid_dir)
        assert r.passed
        assert "all 4" in r.detail

    def test_detects_missing(self, good_backup):
        _, udid_dir = good_backup
        (udid_dir / "Manifest.plist").unlink()
        r = vb.check_structure(udid_dir)
        assert not r.passed
        assert "missing" in r.detail
        assert "Manifest.plist" in r.detail

    def test_detects_empty_file(self, good_backup):
        _, udid_dir = good_backup
        (udid_dir / "Status.plist").write_bytes(b"")
        r = vb.check_structure(udid_dir)
        assert not r.passed
        assert "empty" in r.detail

    def test_detects_nul_shredded(self, shredded_backup):
        """The exact bug: files of right size but all NULs."""
        _, udid_dir = shredded_backup
        r = vb.check_structure(udid_dir)
        assert not r.passed
        assert "bad magic" in r.detail
        # Both plists and the db get flagged
        assert "Manifest.plist" in r.detail
        assert "Manifest.db" in r.detail


# ─── Level 2: status_plist ─────────────────────────────────────────────────

class TestCheckStatusPlist:
    def test_passes_on_finished(self, good_backup):
        _, udid_dir = good_backup
        r = vb.check_status_plist(udid_dir)
        assert r.passed
        assert "finished" in r.detail

    def test_fails_on_unfinished(self, incomplete_backup):
        _, udid_dir = incomplete_backup
        r = vb.check_status_plist(udid_dir)
        assert not r.passed
        assert "SnapshotState" in r.detail
        assert "uploading" in r.detail

    def test_unparseable_plist(self, good_backup):
        _, udid_dir = good_backup
        (udid_dir / "Status.plist").write_bytes(b"not a plist at all")
        r = vb.check_status_plist(udid_dir)
        assert not r.passed
        assert "unparseable" in r.detail

    def test_detects_wrong_backup_state(self, good_backup):
        _, udid_dir = good_backup
        (udid_dir / "Status.plist").write_bytes(plistlib.dumps({
            "BackupState": "old",
            "SnapshotState": "finished",
        }))
        r = vb.check_status_plist(udid_dir)
        assert not r.passed
        assert "BackupState" in r.detail


# ─── Level 3: keybag ───────────────────────────────────────────────────────

class TestCheckKeybag:
    def test_invalid_manifest_plist(self, shredded_backup, monkeypatch):
        """Shredded backup → keybag check fails with clear message."""
        _, udid_dir = shredded_backup
        # Pass a dummy passphrase — won't get used because plist parse fails first
        r = vb.check_keybag(udid_dir, "dummy")
        assert not r.passed
        # Either "corrupt" (caught plistlib explicitly) or generic crypto-rejected
        assert any(k in r.detail.lower() for k in ("corrupt", "manifest", "decrypt", "keybag"))


# ─── Level 4: chatstorage ──────────────────────────────────────────────────

class TestCheckChatstorage:
    def test_fails_on_corrupt_backup(self, shredded_backup):
        _, udid_dir = shredded_backup
        r = vb.check_chatstorage(udid_dir, "dummy")
        assert not r.passed


# ─── run_checks (integration) ──────────────────────────────────────────────

class TestRunChecks:
    def test_stops_at_first_failure(self, shredded_backup):
        _, udid_dir = shredded_backup
        results = vb.run_checks(udid_dir, max_level=4)
        # Should stop at level 1 (structure fail) — not run levels 2-4
        assert len(results) == 1
        assert results[0].name == "structure"
        assert not results[0].passed

    def test_runs_levels_2_after_structure_passes(self, incomplete_backup):
        _, udid_dir = incomplete_backup
        results = vb.run_checks(udid_dir, max_level=2)
        assert len(results) == 2
        assert results[0].passed   # structure OK
        assert not results[1].passed  # status fails

    def test_max_level_caps_run(self, good_backup):
        _, udid_dir = good_backup
        results = vb.run_checks(udid_dir, max_level=1)
        assert len(results) == 1


# ─── CLI ───────────────────────────────────────────────────────────────────

SCRIPT = Path(__file__).parent.parent / "verify_backup.py"


def _run_cli(*args, env=None):
    cmd_env = {} if env is None else env
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30,
        env={**__import__("os").environ, **cmd_env},
    )


class TestCLI:
    def test_help_runs(self):
        result = _run_cli("--help")
        assert result.returncode == 0
        assert "verify" in result.stdout.lower()
        assert "--level" in result.stdout

    def test_invalid_level_rejected(self, good_backup):
        base, _ = good_backup
        result = _run_cli("--backup-dir", str(base), "--level", "9")
        assert result.returncode != 0

    def test_passes_on_good_backup_level1(self, good_backup):
        base, _ = good_backup
        result = _run_cli("--backup-dir", str(base), "--level", "1")
        assert result.returncode == 0
        assert "PASS" in result.stdout or "✓" in result.stdout

    def test_fails_on_shredded_backup(self, shredded_backup):
        base, _ = shredded_backup
        result = _run_cli("--backup-dir", str(base), "--level", "1")
        assert result.returncode == 1

    def test_json_output(self, good_backup):
        base, _ = good_backup
        result = _run_cli("--backup-dir", str(base), "--level", "2", "--json")
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["all_passed"] is True
        assert data["checks_run"] == 2
        assert data["results"][0]["name"] == "structure"

    def test_no_backup_env_exits_with_2(self, tmp_path):
        # No env, no --backup-dir → exit 2 (env problem, not check fail)
        env = {"MIKOSHI_BACKUP_DIR": ""}  # explicitly blank
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, timeout=10,
            env={k: v for k, v in __import__("os").environ.items()
                 if k != "MIKOSHI_BACKUP_DIR"},
        )
        assert result.returncode == 2
        assert "MIKOSHI_BACKUP_DIR" in result.stderr or "no backup" in result.stderr.lower()
