"""Tests for mikoshi-whatsapp.sh — exercise dispatch + has_favorites logic.

We don't actually trigger backups; we stub out PIPELINE so the wrapper runs
fast and we can assert which args it forwarded.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).parent.parent / "mikoshi-whatsapp.sh"


def _run(args, env_extra=None, stdin=None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(WRAPPER)] + args,
        env=env, capture_output=True, text=True, input=stdin, timeout=15,
    )


@pytest.fixture
def stub_pipeline(tmp_path, monkeypatch):
    """
    Replace run_pipeline.sh with a script that logs its args and exits 0.
    Returns a path; read it to see what args the wrapper passed.

    Also provides a stub `pipeline_state.py` in the fake root so the
    wrapper's smart-phase detection (`python3 -m pipeline_state best-phase`)
    has something to import. The stub always reports phase=4 + iPhone
    reachable, which lets the pipeline path actually run.
    """
    stub = tmp_path / "run_pipeline.sh"
    args_log = tmp_path / "args.txt"
    stub.write_text(
        f'#!/bin/bash\necho "$@" > {args_log}\nexit 0\n'
    )
    stub.chmod(0o755)
    fake_root = tmp_path / "fake_export"
    fake_root.mkdir()
    (fake_root / "mikoshi-whatsapp.sh").symlink_to(WRAPPER)
    (fake_root / "run_pipeline.sh").write_text(stub.read_text())
    (fake_root / "run_pipeline.sh").chmod(0o755)
    (fake_root / "tui.py").write_text("print('TUI stub')\n")
    venv = fake_root / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "activate").write_text("# stub venv\n")

    # Stub pipeline_state.py with the minimum CLI surface the wrapper
    # actually invokes:
    #   - `best-phase` (smart-phase detection)
    #   - `check-server-cursor` (pre-flight before sync)
    # The real module does iPhone detection + a live HTTP probe; the
    # stub returns predictable values so wrapper tests are deterministic.
    # Phase=4 means "everything is cached, run extract."
    (fake_root / "pipeline_state.py").write_text(
        "import sys\n"
        "if len(sys.argv) >= 2 and sys.argv[1] == 'best-phase':\n"
        "    if '--require-iphone' in sys.argv:\n"
        "        sys.exit(0)\n"
        "    print('4\\tStub (test fixture)')\n"
        "    sys.exit(0)\n"
        "if len(sys.argv) >= 2 and sys.argv[1] == 'check-server-cursor':\n"
        "    print('server cursor OK (stub)')\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n"
    )

    monkeypatch.setattr("os.environ", {**os.environ, "MIKOSHI_INGEST_CONF": str(tmp_path / "no-conf")})
    return {"wrapper": fake_root / "mikoshi-whatsapp.sh", "args_log": args_log,
            "favorites_file": tmp_path / "favs.json"}


def _run_stub(stub, subargs, favorites=None, env_extra=None):
    env = os.environ.copy()
    # Don't let the developer's real MIKOSHI_BACKUP_DIR leak into the stub —
    # the redesigned cron path runs `python3 -m pipeline_state best-phase`
    # against it, which would auto-inject --from-phase based on whatever
    # backup happens to be on the dev's external SSD. Tests need a clean
    # slate; force best-phase to default to Phase 1.
    env.pop("MIKOSHI_BACKUP_DIR", None)
    if env_extra:
        env.update(env_extra)
    env["MIKOSHI_FAVORITES_FILE"] = str(stub["favorites_file"])
    env["MIKOSHI_INGEST_CONF"] = str(stub["favorites_file"].parent / "nonexistent.conf")
    if favorites is None:
        stub["favorites_file"].unlink(missing_ok=True)
    else:
        stub["favorites_file"].write_text(json.dumps({
            "version": 1, "favorites": favorites,
        }))
    return subprocess.run(
        ["bash", str(stub["wrapper"])] + subargs,
        env=env, capture_output=True, text=True, timeout=15,
    )


# ─── --help ────────────────────────────────────────────────────────────────

class TestHelp:
    def test_help_flag(self):
        result = _run(["--help"])
        assert result.returncode == 0
        assert "Subcommands" in result.stdout
        assert "tui" in result.stdout
        assert "sync" in result.stdout
        assert "favorites" in result.stdout.lower()

    def test_h_shorthand(self):
        result = _run(["-h"])
        assert result.returncode == 0
        assert "Subcommands" in result.stdout

    def test_help_shows_cron_example(self):
        result = _run(["--help"])
        assert "cron" in result.stdout.lower()

    def test_help_lists_reset_backup(self):
        result = _run(["--help"])
        assert "reset-backup" in result.stdout


# ─── unknown subcommand ────────────────────────────────────────────────────

class TestUnknownSubcommand:
    def test_unknown_exits_with_error(self):
        result = _run(["weird-thing"])
        assert result.returncode == 1
        assert "Unknown" in result.stdout or "Unknown" in result.stderr


# ─── sync dispatch ─────────────────────────────────────────────────────────

class TestSyncDispatch:
    def test_sync_no_favorites_falls_back_to_all(self, stub_pipeline):
        result = _run_stub(stub_pipeline, ["sync", "--skip-remote-sync"], favorites=None)
        assert result.returncode == 0
        forwarded = stub_pipeline["args_log"].read_text().strip()
        # --favorites NOT in args; --skip-remote-sync IS
        assert "--favorites" not in forwarded
        assert "--skip-remote-sync" in forwarded
        assert "no favorites file" in result.stdout

    def test_sync_with_favorites_adds_flag(self, stub_pipeline):
        result = _run_stub(
            stub_pipeline, ["sync", "--skip-remote-sync"],
            favorites=[{"jid": "alice@s.whatsapp.net", "name": "Alice"}],
        )
        assert result.returncode == 0
        forwarded = stub_pipeline["args_log"].read_text().strip()
        assert "--favorites" in forwarded
        assert "favorites detected" in result.stdout

    def test_sync_all_ignores_favorites(self, stub_pipeline):
        """Even with favorites set, --all forces full incremental."""
        result = _run_stub(
            stub_pipeline, ["sync", "--all", "--skip-remote-sync"],
            favorites=[{"jid": "alice@s.whatsapp.net", "name": "Alice"}],
        )
        assert result.returncode == 0
        forwarded = stub_pipeline["args_log"].read_text().strip()
        assert "--favorites" not in forwarded

    def test_sync_full_passes_mode_full(self, stub_pipeline):
        result = _run_stub(
            stub_pipeline, ["sync", "--full", "--skip-remote-sync"], favorites=None
        )
        assert result.returncode == 0
        forwarded = stub_pipeline["args_log"].read_text().strip()
        assert "--mode full" in forwarded

    def test_sync_propagates_exit_code(self, stub_pipeline):
        """Make the stub fail and ensure wrapper exits non-zero."""
        # Overwrite stub to fail
        (stub_pipeline["wrapper"].parent / "run_pipeline.sh").write_text(
            "#!/bin/bash\nexit 42\n"
        )
        result = _run_stub(stub_pipeline, ["sync", "--skip-remote-sync"], favorites=None)
        assert result.returncode == 42

    def test_empty_favorites_treated_as_no_favorites(self, stub_pipeline):
        result = _run_stub(stub_pipeline, ["sync", "--skip-remote-sync"], favorites=[])
        assert result.returncode == 0
        forwarded = stub_pipeline["args_log"].read_text().strip()
        assert "--favorites" not in forwarded
        assert "no favorites file" in result.stdout

    def test_sync_all_with_no_other_args(self, stub_pipeline):
        """Regression: bash 3.2 + set -u crashes on empty array expansion.

        `sync --all` with no other flags previously emitted
        'args[@]: unbound variable' because `${args[@]}` is not a safe
        expansion when the array is empty.

        Post-redesign: when no backup + no iPhone, the wrapper exits
        cleanly with rc=0 ("nothing to do") instead of crashing the cron
        run. Either way: no unbound-variable explosion, and `--favorites`
        must not appear (no favorites file present).
        """
        result = _run_stub(stub_pipeline, ["sync", "--all"], favorites=None)
        assert result.returncode == 0, (
            f"sync --all crashed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "unbound variable" not in result.stderr
        # If the pipeline was invoked, it must not have been with --favorites.
        if stub_pipeline["args_log"].exists():
            forwarded = stub_pipeline["args_log"].read_text().strip()
            assert "--favorites" not in forwarded

    def test_sync_with_no_args_at_all(self, stub_pipeline):
        """Plain `sync` without favorites and without flags."""
        result = _run_stub(stub_pipeline, ["sync"], favorites=None)
        assert result.returncode == 0
        assert "unbound variable" not in result.stderr

    def test_sync_full_alone(self, stub_pipeline):
        """sync --full alone: args has 2 elements (--mode, full), not empty."""
        result = _run_stub(stub_pipeline, ["sync", "--full"], favorites=None)
        assert result.returncode == 0
        forwarded = stub_pipeline["args_log"].read_text().strip()
        assert "--mode" in forwarded and "full" in forwarded


# ─── status / tui subcommand presence ──────────────────────────────────────

class TestResetBackup:
    def _setup_fake_backup(self, tmp_path, monkeypatch):
        """Create a fake backup tree and stub script."""
        fake_root = tmp_path / "fake_export"
        fake_root.mkdir()
        (fake_root / "mikoshi-whatsapp.sh").symlink_to(WRAPPER)
        (fake_root / "run_pipeline.sh").write_text("#!/bin/bash\nexit 0\n")
        (fake_root / "run_pipeline.sh").chmod(0o755)
        (fake_root / "tui.py").write_text("print('stub')\n")
        venv = fake_root / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "activate").write_text("# stub\n")

        # Backup tree with a UDID-like dir
        backup_dir = tmp_path / "backup_root"
        udid_dir = backup_dir / "backup" / "00008130-0001184C1E46001C"
        udid_dir.mkdir(parents=True)
        (udid_dir / "Manifest.plist").write_bytes(b"\x00" * 1024)
        (udid_dir / "stale.bin").write_bytes(b"x" * 1024)

        return fake_root, backup_dir, udid_dir

    def test_aborts_when_no_yes(self, tmp_path):
        fake_root, backup_dir, udid_dir = self._setup_fake_backup(tmp_path, None)
        env = os.environ.copy()
        env["MIKOSHI_BACKUP_DIR"] = str(backup_dir)
        result = subprocess.run(
            ["bash", str(fake_root / "mikoshi-whatsapp.sh"), "reset-backup"],
            env=env, capture_output=True, text=True, input="no\n", timeout=10,
        )
        assert result.returncode == 1
        assert udid_dir.exists(), "Should not have deleted without confirmation"
        assert "aborted" in result.stdout

    def test_deletes_on_yes(self, tmp_path):
        fake_root, backup_dir, udid_dir = self._setup_fake_backup(tmp_path, None)
        env = os.environ.copy()
        env["MIKOSHI_BACKUP_DIR"] = str(backup_dir)
        result = subprocess.run(
            ["bash", str(fake_root / "mikoshi-whatsapp.sh"), "reset-backup"],
            env=env, capture_output=True, text=True, input="yes\n", timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert not udid_dir.exists()
        assert "removed" in result.stdout

    def test_force_skips_prompt(self, tmp_path):
        fake_root, backup_dir, udid_dir = self._setup_fake_backup(tmp_path, None)
        env = os.environ.copy()
        env["MIKOSHI_BACKUP_DIR"] = str(backup_dir)
        result = subprocess.run(
            ["bash", str(fake_root / "mikoshi-whatsapp.sh"), "reset-backup", "--force"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert not udid_dir.exists()

    def test_no_backup_present(self, tmp_path):
        fake_root, backup_dir, _ = self._setup_fake_backup(tmp_path, None)
        # Nuke the UDID dir manually first
        import shutil
        shutil.rmtree(backup_dir / "backup")
        env = os.environ.copy()
        env["MIKOSHI_BACKUP_DIR"] = str(backup_dir)
        result = subprocess.run(
            ["bash", str(fake_root / "mikoshi-whatsapp.sh"), "reset-backup", "--force"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "nothing to reset" in result.stdout

    def test_preserves_sibling_dirs(self, tmp_path):
        """reset-backup must NOT touch other files in MIKOSHI_BACKUP_DIR."""
        fake_root, backup_dir, udid_dir = self._setup_fake_backup(tmp_path, None)
        sibling = backup_dir / "my-other-data.txt"
        sibling.write_text("important")

        env = os.environ.copy()
        env["MIKOSHI_BACKUP_DIR"] = str(backup_dir)
        subprocess.run(
            ["bash", str(fake_root / "mikoshi-whatsapp.sh"), "reset-backup", "--force"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert sibling.exists()
        assert sibling.read_text() == "important"


class TestDiagnoseBackupError:
    """Test the bash-level diagnose function by invoking run_pipeline.sh with
    crafted log files. We can't run a real backup, but we can sneak in a
    fake log via a shell wrapper that sources the function."""

    def _diagnose(self, log_content):
        """Source run_pipeline.sh's diagnose function and run it on log_content."""
        with subprocess.Popen(
            ["bash", "-c", f"""
                set +e
                # Stub out functions that diagnose_backup_error doesn't need
                error() {{ echo "$@" >&2; }}
                # Define TEMP_DIR_IS_EXTERNAL so the conditional branch works
                TEMP_DIR_IS_EXTERNAL=true
                TEMP_DIR=/tmp/fake
                # Source the function from run_pipeline.sh
                eval "$(sed -n '/^diagnose_backup_error()/,/^}}/p' {Path(__file__).parent.parent / 'run_pipeline.sh'})"
                # Create the log file
                tmp=$(mktemp)
                cat > "$tmp" <<'LOGEOF'
{log_content}
LOGEOF
                diagnose_backup_error "$tmp"
                rm -f "$tmp"
            """],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as proc:
            out, err = proc.communicate(timeout=10)
            return err

    def test_status_plist_corruption_detected(self):
        log = (
            "Starting backup...\n"
            "Sending '00008130-XXX/Status.plist' (4.1 KB)\n"
            "ErrorCode 205: Error reading status (MBErrorDomain/205). "
            "Underlying error: Error deserializing property list: Unexpected character"
        )
        err = self._diagnose(log)
        assert "CORRUPT" in err or "corrupt" in err.lower()
        assert "reset-backup" in err

    def test_info_plist_corruption_detected(self):
        log = "Could not read Info.plist"
        err = self._diagnose(log)
        assert "CORRUPT" in err or "corrupt" in err.lower()

    def test_locked_device_detected(self):
        log = "device is locked - cannot backup"
        err = self._diagnose(log)
        assert "locked" in err.lower()

    def test_trust_not_established(self):
        log = "User did not trust this computer"
        err = self._diagnose(log)
        assert "Trust" in err or "trust" in err.lower()


class TestSubcommandPresence:
    def test_status_subcommand_exists(self):
        # status would try to call tui.action_status; without a backup it may
        # warn but should not be "Unknown subcommand"
        result = _run(["status"], env_extra={
            "MIKOSHI_INGEST_CONF": "/tmp/nonexistent-conf-for-test.conf",
        })
        # We accept any rc — what matters is that the wrapper recognised the cmd
        assert "Unknown subcommand" not in (result.stdout + result.stderr)

    def test_tui_subcommand_recognised(self):
        # tui would block on stdin in interactive mode. We don't actually run
        # it; we just check the dispatch resolves it (here by checking --help
        # is the only safe verifiable path, so this test is a smoke check).
        result = _run(["--help"])
        assert "tui" in result.stdout
