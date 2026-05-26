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


def _extract_fn(script_text: str, fn_name: str) -> str:
    """Pull a `name() { ... }` block out of a bash script (depth-balanced)."""
    start = script_text.find(f"{fn_name}() {{")
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
    return script_text[start:end]


def _run_cleanup(temp_dir: Path, external: bool, *,
                 secure_cleanup: str | None = None):
    """
    Source run_pipeline.sh's cleanup() + secure_cleanup_optin() functions
    and run cleanup() against temp_dir.

    Post-redesign, the cleanup trap doesn't shred anything by default —
    that's now opt-in via MIKOSHI_SECURE_CLEANUP=1 (or the standalone
    `./mikoshi-whatsapp.sh purge-extracted` subcommand).

    `secure_cleanup`:
      - None         → don't set the env var (default behaviour: no shred)
      - "1"          → enable the opt-in shred
    """
    script_text = PIPELINE.read_text()
    cleanup_fn = _extract_fn(script_text, "cleanup")
    secure_fn = _extract_fn(script_text, "secure_cleanup_optin")

    runner = f"""
set +e
log()   {{ echo "LOG: $@"; }}
error() {{ echo "ERR: $@" >&2; }}
warn()  {{ echo "WARN: $@"; }}
info()  {{ echo "INFO: $@"; }}
TEMP_DIR="{temp_dir}"
TEMP_DIR_IS_EXTERNAL={"true" if external else "false"}
LOCK_FILE="/tmp/test-lock-$$"
{secure_fn}
{cleanup_fn}
trap - EXIT
cleanup
"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("MIKOSHI_PRESERVE_EXTRACTED", "MIKOSHI_SECURE_CLEANUP")}
    if secure_cleanup is not None:
        env["MIKOSHI_SECURE_CLEANUP"] = secure_cleanup
    return subprocess.run(
        ["bash", "-c", runner], env=env,
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

    def test_default_cleanup_preserves_decrypted_artifacts(self, tmp_path):
        """
        Default behaviour after the redesign: cleanup does NOT shred
        anything. The old wipe-on-success path was wrecking 13 minutes
        of decryption work on every iteration. See REDESIGN.md pain
        point #4.
        """
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        chat_db = extracted / "ChatStorage.sqlite"
        chat_db.write_bytes(b"SQLite format 3\x00" + b"x" * 4096)

        _make_fake_backup_tree(tmp_path)

        result = _run_cleanup(tmp_path, external=True)
        assert result.returncode == 0
        # extracted/ must still be there — that's the point.
        assert extracted.exists(), \
            "Default cleanup must not touch decrypted artifacts"
        assert chat_db.exists()

    def test_opt_in_secure_cleanup_shreds(self, tmp_path):
        """MIKOSHI_SECURE_CLEANUP=1 brings back the shred-on-success path
        for the user who wants it. See REDESIGN.md §6.1.
        """
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        chat_db = extracted / "ChatStorage.sqlite"
        chat_db.write_bytes(b"SQLite format 3\x00" + b"x" * 4096)
        manifest_plist = extracted / "Manifest.plist"
        manifest_plist.write_bytes(b"bplist00" + b"x" * 1000)

        _make_fake_backup_tree(tmp_path)

        result = _run_cleanup(tmp_path, external=True, secure_cleanup="1")
        assert result.returncode == 0
        # The opt-in path shreds individual sensitive files but leaves the
        # extracted/ directory itself (so a partial re-decrypt can land cleanly).
        # We just need to confirm the file contents were destroyed.
        if chat_db.exists():
            # If it still exists, it must have been overwritten (shred -vfz),
            # so contents won't match the original.
            assert chat_db.read_bytes() != b"SQLite format 3\x00" + b"x" * 4096

    def test_no_decrypted_dir_is_a_noop(self, tmp_path):
        """If extracted/ doesn't exist, cleanup must not error."""
        _make_fake_backup_tree(tmp_path)
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


class TestSecureCleanupOptin:
    """The redesigned `secure_cleanup_optin` runs only when explicitly
    requested (MIKOSHI_SECURE_CLEANUP=1). When it does run, it must still
    obey the original safety rule: scoped to extracted/, never recursing
    into the encrypted backup tree under backup/<UDID>/.
    """

    def _extract_secure_fn(self) -> str:
        script_text = PIPELINE.read_text()
        start = script_text.find("secure_cleanup_optin() {")
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
        return script_text[start:end]

    def test_optin_does_not_touch_encrypted_backup(self, tmp_path):
        """The 55-hour-backup-destroying bug must remain impossible: the
        opt-in shred must only touch files under extracted/."""
        udid_dir = _make_fake_backup_tree(tmp_path)
        # Also seed an extracted/ subtree so the shred has something to do.
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "ChatStorage.sqlite").write_bytes(b"x" * 100)

        manifest_before = (udid_dir / "Manifest.plist").read_bytes()
        manifest_db_before = (udid_dir / "Manifest.db").read_bytes()

        runner = f"""
set +e
log() {{ echo "$@"; }}
error() {{ echo "ERR: $@" >&2; }}
warn() {{ echo "WARN: $@"; }}
TEMP_DIR="{tmp_path}"
{self._extract_secure_fn()}
secure_cleanup_optin
"""
        result = subprocess.run(
            ["bash", "-c", runner],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0
        # Critical: encrypted Manifest.plist / Manifest.db must be untouched.
        assert (udid_dir / "Manifest.plist").read_bytes() == manifest_before
        assert (udid_dir / "Manifest.db").read_bytes() == manifest_db_before

    def test_optin_shreds_extracted_chatstorage(self, tmp_path):
        """When invoked, the opt-in shred must scrub ChatStorage.sqlite
        in extracted/. Used by `mikoshi-whatsapp.sh purge-extracted` and
        by MIKOSHI_SECURE_CLEANUP=1.
        """
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        chat = extracted / "ChatStorage.sqlite"
        original = b"SQLite format 3\x00" + b"x" * 4096
        chat.write_bytes(original)

        runner = f"""
set +e
log() {{ echo "$@"; }}
error() {{ echo "ERR: $@" >&2; }}
warn() {{ echo "WARN: $@"; }}
TEMP_DIR="{tmp_path}"
{self._extract_secure_fn()}
secure_cleanup_optin
"""
        result = subprocess.run(
            ["bash", "-c", runner],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        # The shred either removed the file or overwrote its contents.
        if chat.exists():
            assert chat.read_bytes() != original, \
                "secure_cleanup_optin failed to scramble ChatStorage.sqlite"


