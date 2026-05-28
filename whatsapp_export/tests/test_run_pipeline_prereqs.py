"""Integration tests for `run_pipeline.sh` --from-phase prerequisite checks.

The prereq block at the top of `run_pipeline.sh:main` decides whether to
abort because required artifacts are missing. Historically it over-applied
the encrypted-UDID check to phase 4, which broke the LaunchAgent path
(phase 4 only needs the *decrypted* ChatStorage).

These tests build a controlled tmp environment with only the artifacts
each phase actually needs and assert the wrapper either succeeds (gets
past the prereq block) or fails with the right error.

To keep tests fast we short-circuit at the point the wrapper would
hand off to the heavy Python extractor: we replace `extract_messages.py`
with a no-op stub so the test exercises only the prereq decision logic.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
REAL_PIPELINE = PROJECT_ROOT / "run_pipeline.sh"


@pytest.fixture
def fake_project(tmp_path):
    """Build a fake whatsapp_export/ root with the real run_pipeline.sh
    plus stubbed Python entrypoints. Returns paths the tests can poke."""
    root = tmp_path / "fake_export"
    root.mkdir()

    # Copy the real wrapper bash scripts — we want the production logic
    # under test, not a stub.
    shutil.copy(REAL_PIPELINE, root / "run_pipeline.sh")
    (root / "run_pipeline.sh").chmod(0o755)
    (root / "mikoshi-whatsapp.sh").write_text("#!/bin/bash\nexit 0\n")
    (root / "mikoshi-whatsapp.sh").chmod(0o755)

    # Minimal Python stubs — extract_messages produces an export file
    # at $EXPORT_FILE; push_via_api is a no-op.
    (root / "extract_messages.py").write_text(
        "import sys, argparse, json\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--output', required=True)\n"
        "args, _ = p.parse_known_args()\n"
        "open(args.output, 'w').write(json.dumps({\n"
        "    'schema_version': '1.2', 'chats': [], 'attachments_index': {},\n"
        "}))\n"
    )
    (root / "validate_export.py").write_text("import sys; sys.exit(0)\n")
    (root / "push_via_api.py").write_text("import sys; sys.exit(0)\n")
    (root / "schema.json").write_text("{}\n")
    (root / "secure_cleanup.py").write_text("import sys; sys.exit(0)\n")

    # Fake venv so activate_venv succeeds
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("# stub\n")

    # External "backup root" — phase 4 should NOT require anything in here
    backup_root = tmp_path / "backup_root"
    backup_root.mkdir()

    # Decrypted ChatStorage that phase 4 DOES require
    extracted = backup_root / "extracted"
    extracted.mkdir()
    chat_storage = extracted / "ChatStorage.sqlite"
    chat_storage.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

    return {
        "root": root,
        "pipeline": root / "run_pipeline.sh",
        "backup_root": backup_root,
        "chat_storage": chat_storage,
        "backup_dir": backup_root / "backup",
        "exports": root / "exports",
        "logs": root / "logs",
    }


def _run_pipeline(fake_project, extra_args, env_extra=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(fake_project["root"].parent),
        "MIKOSHI_BACKUP_DIR": str(fake_project["backup_root"]),
        # Skip the server-cursor pre-flight; that has its own tests
        "MIKOSHI_TRUST_LOCAL_CURSOR": "1",
        # Bypass remote auth entirely
        "MIKOSHI_URL": "http://stub.invalid",
        "MIKOSHI_TOKEN": "stub-token",
        # Empty conf so wrapper doesn't look at the dev's real config
        "MIKOSHI_INGEST_CONF": "/tmp/__test_no_conf__",
    }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(fake_project["pipeline"]),
         "--skip-remote-sync"] + list(extra_args),
        env=env, capture_output=True, text=True, timeout=30,
    )


class TestPhase4SkipsUdidCheck:
    """Regression: --from-phase 4 used to demand $BACKUP_PATH/<UDID>/ to
    exist, breaking the LaunchAgent cron path on Macs where the encrypted
    backup directory got cleaned up but the decrypted ChatStorage was
    preserved (MIKOSHI_PRESERVE_EXTRACTED=true).
    """

    def test_phase_4_succeeds_without_udid_directory(self, fake_project):
        # No UDID dir exists; only the decrypted ChatStorage. Phase 4
        # should still work because it reads the decrypted DB directly.
        assert not fake_project["backup_dir"].exists()
        result = _run_pipeline(fake_project, ["--from-phase", "4"])
        # The old code would have exited 1 here with "needs <UDID>/ to exist"
        if "needs " in result.stdout and "UDID" in result.stdout:
            pytest.fail(
                f"Wrapper still demands UDID for phase 4:\n"
                f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
            )
        # And it must have gotten past the prereq block (the pipeline
        # log emits "Reusing decrypted ChatStorage" once the check passes)
        assert "Reusing decrypted ChatStorage" in result.stdout or result.returncode == 0

    def test_phase_4_still_fails_when_chatstorage_missing(self, fake_project):
        # Remove the only thing phase 4 actually needs
        fake_project["chat_storage"].unlink()
        result = _run_pipeline(fake_project, ["--from-phase", "4"])
        assert result.returncode != 0
        # The right failure message — about ChatStorage, NOT UDID
        assert "ChatStorage.sqlite" in result.stdout or "ChatStorage.sqlite" in result.stderr

    def test_phase_3_still_requires_udid(self, fake_project):
        # Phase 3 is the decrypt phase — it genuinely needs the encrypted
        # backup. Make sure we didn't accidentally relax its check too.
        assert not fake_project["backup_dir"].exists()
        result = _run_pipeline(fake_project, ["--from-phase", "3"])
        assert result.returncode != 0
        out = result.stdout + result.stderr
        assert "UDID" in out or "needs " in out


class TestPhase4MacOnlyDoesntDemandIPhonePaths:
    """Companion to the C5 change: Mac-only sync (MIKOSHI_SOURCES=mac_live)
    must work even when no iPhone backup OR decrypted ChatStorage exists
    — extract reads from the live Mac DB directly."""

    def test_phase_4_mac_only_no_iphone_artifacts(self, fake_project):
        fake_project["chat_storage"].unlink()  # remove iPhone-side artifact
        result = _run_pipeline(
            fake_project, ["--from-phase", "4"],
            env_extra={"MIKOSHI_SOURCES": "mac_live"},
        )
        # Mac-only path is allowed to proceed without iPhone ChatStorage
        if "ChatStorage" in result.stdout + result.stderr and \
           "needs " in result.stdout + result.stderr:
            pytest.fail(
                f"Mac-only --from-phase 4 wrongly demands iPhone ChatStorage:\n"
                f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
            )
