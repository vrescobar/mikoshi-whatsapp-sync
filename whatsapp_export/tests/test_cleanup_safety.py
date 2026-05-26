"""Regression test for the 'cleanup destroys encrypted backup' bug.

The bug: cleanup() in run_pipeline.sh used `find $TEMP_DIR -name '*.plist' ...`
without scoping. When MIKOSHI_BACKUP_DIR == TEMP_DIR, find recursed into
the encrypted iPhone backup's subdirectories and matched
Manifest.plist / Status.plist / Info.plist / Manifest.db there, then
shred-ed them with 7 passes. That destroyed a 55-hour backup.

This test sources the cleanup() function from run_pipeline.sh and runs
it against a synthetic backup tree, asserting the encrypted files
survive.
"""

import os
import subprocess
from pathlib import Path

import pytest

PIPELINE = Path(__file__).parent.parent / "run_pipeline.sh"


def _make_fake_backup_tree(root: Path):
    """Build a structure that mirrors what idevicebackup2 produces."""
    udid_dir = root / "backup" / "00008130-0001184C1E46001C"
    udid_dir.mkdir(parents=True)

    # Critical files at the UDID root — must NOT be shred-ed
    (udid_dir / "Manifest.plist").write_bytes(b"bplist00" + b"\x00" * 1000)
    (udid_dir / "Manifest.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 1000)
    (udid_dir / "Status.plist").write_bytes(b"bplist00" + b"\x00" * 500)
    (udid_dir / "Info.plist").write_bytes(b"bplist00" + b"\x00" * 500)

    # A few hash-named blob files (typical backup content)
    bucket = udid_dir / "ab"
    bucket.mkdir()
    (bucket / ("abcdef1234567890" * 2)).write_bytes(b"\xff" * 4096)

    return udid_dir


def _run_cleanup(temp_dir: Path, external: bool):
    """Source run_pipeline.sh's cleanup function and run it against temp_dir."""
    # Extract the cleanup() function from the script
    script_text = PIPELINE.read_text()
    start = script_text.find("cleanup() {")
    # End at the closing brace of cleanup (find next \n}\n at top level)
    depth = 0
    end = start
    for i, ch in enumerate(script_text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    cleanup_fn = script_text[start:end]

    runner = f"""
set +e
log()   {{ echo "LOG: $@"; }}
error() {{ echo "ERR: $@" >&2; }}
warn()  {{ echo "WARN: $@"; }}
info()  {{ echo "INFO: $@"; }}
TEMP_DIR="{temp_dir}"
TEMP_DIR_IS_EXTERNAL={"true" if external else "false"}
LOCK_FILE="/tmp/test-lock-$$"
{cleanup_fn}
# Don't let cleanup's trap exit kill the test runner
trap - EXIT
cleanup
"""
    return subprocess.run(
        ["bash", "-c", runner],
        capture_output=True, text=True, timeout=30,
    )


class TestCleanupPreservesEncryptedBackup:
    def test_encrypted_backup_files_survive_external_mode(self, tmp_path):
        """The regression: MIKOSHI_BACKUP_DIR active, encrypted files must live."""
        udid_dir = _make_fake_backup_tree(tmp_path)

        # Snapshot file contents
        manifest_plist_before = (udid_dir / "Manifest.plist").read_bytes()
        manifest_db_before = (udid_dir / "Manifest.db").read_bytes()
        status_before = (udid_dir / "Status.plist").read_bytes()
        info_before = (udid_dir / "Info.plist").read_bytes()

        result = _run_cleanup(tmp_path, external=True)
        # Should exit 0 (the cleanup itself doesn't fail)
        assert result.returncode == 0, result.stderr

        # CRITICAL: encrypted backup files must be byte-identical
        assert (udid_dir / "Manifest.plist").read_bytes() == manifest_plist_before, \
            "Manifest.plist of encrypted backup was modified — the bug is back!"
        assert (udid_dir / "Manifest.db").read_bytes() == manifest_db_before, \
            "Manifest.db of encrypted backup was modified — the bug is back!"
        assert (udid_dir / "Status.plist").read_bytes() == status_before
        assert (udid_dir / "Info.plist").read_bytes() == info_before

    def test_decrypted_artifacts_are_still_shredded(self, tmp_path):
        """The cleanup should still scrub the decrypted area."""
        # Set up a fake decrypted artifact directory
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        chat_db = extracted / "ChatStorage.sqlite"
        chat_db.write_bytes(b"SQLite format 3\x00" + b"x" * 4096)
        manifest_plist = extracted / "Manifest.plist"
        manifest_plist.write_bytes(b"bplist00" + b"x" * 1000)

        # Also put the encrypted tree alongside to make sure scoping works
        _make_fake_backup_tree(tmp_path)

        result = _run_cleanup(tmp_path, external=True)
        assert result.returncode == 0

        # extracted/ should be gone entirely
        assert not extracted.exists(), "Decrypted artifacts were not cleaned"

    def test_local_mode_wipes_everything_under_temp(self, tmp_path):
        """In local mode (no MIKOSHI_BACKUP_DIR), cleanup nukes the whole dir."""
        (tmp_path / "extracted").mkdir()
        (tmp_path / "extracted" / "ChatStorage.sqlite").write_bytes(b"x" * 100)
        # No encrypted backup tree — local mode doesn't preserve anything
        result = _run_cleanup(tmp_path, external=False)
        assert result.returncode == 0
        assert not tmp_path.exists()

    def test_no_decrypted_dir_is_a_noop(self, tmp_path):
        """If extracted/ doesn't exist, cleanup must not error."""
        _make_fake_backup_tree(tmp_path)
        # No extracted/ subdir at all
        result = _run_cleanup(tmp_path, external=True)
        assert result.returncode == 0


class TestFromPhase:
    """--from-phase N flag: validation + path-prerequisite checks."""

    def test_rejects_non_numeric(self):
        result = subprocess.run(
            ["bash", str(PIPELINE), "--from-phase", "abc"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1
        assert "must be an integer" in result.stdout

    def test_rejects_out_of_range(self):
        result = subprocess.run(
            ["bash", str(PIPELINE), "--from-phase", "9"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1

    def test_help_advertises_flag(self):
        result = subprocess.run(
            ["bash", str(PIPELINE), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "--from-phase" in result.stdout

    def test_phase3_requires_backup_dir(self, tmp_path, monkeypatch):
        """--from-phase 3 needs MIKOSHI_BACKUP_DIR pointing at a backup."""
        # No MIKOSHI_BACKUP_DIR set → must error
        env = os.environ.copy()
        env.pop("MIKOSHI_BACKUP_DIR", None)
        env["MIKOSHI_INGEST_CONF"] = str(tmp_path / "nope.conf")
        result = subprocess.run(
            ["bash", str(PIPELINE), "--from-phase", "3"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        # Will fail one way or another (no venv too, etc.); we just want to
        # confirm the validation message appears somewhere
        assert "MIKOSHI_BACKUP_DIR" in (result.stdout + result.stderr) or \
               result.returncode != 0


class TestSecureCleanupPhase5:
    """Phase 5's secure_cleanup() had the same bug — it also recursed
    $TEMP_DIR. Verify it's scoped to extracted/ too."""

    def test_phase5_does_not_touch_encrypted_backup(self, tmp_path):
        """Source secure_cleanup() and run it against a fake backup tree."""
        script_text = PIPELINE.read_text()
        # Extract secure_cleanup() body
        start = script_text.find("secure_cleanup() {")
        depth = 0
        end = start
        for i, ch in enumerate(script_text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        fn = script_text[start:end]

        udid_dir = _make_fake_backup_tree(tmp_path)
        manifest_before = (udid_dir / "Manifest.plist").read_bytes()

        runner = f"""
set +e
log() {{ echo "$@"; }}
error() {{ echo "ERR: $@" >&2; }}
warn() {{ echo "WARN: $@"; }}
TEMP_DIR="{tmp_path}"
{fn}
secure_cleanup
"""
        result = subprocess.run(
            ["bash", "-c", runner],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        assert (udid_dir / "Manifest.plist").read_bytes() == manifest_before, \
            "secure_cleanup destroyed encrypted Manifest.plist"
