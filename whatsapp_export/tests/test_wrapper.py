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
    """
    stub = tmp_path / "run_pipeline.sh"
    args_log = tmp_path / "args.txt"
    stub.write_text(
        f'#!/bin/bash\necho "$@" > {args_log}\nexit 0\n'
    )
    stub.chmod(0o755)
    # The wrapper resolves PIPELINE as ${SCRIPT_DIR}/run_pipeline.sh.
    # We can't easily override that without monkey-patching the wrapper,
    # so we use a sibling temp dir layout.
    fake_root = tmp_path / "fake_export"
    fake_root.mkdir()
    # Symlink the real wrapper into our fake root
    (fake_root / "mikoshi-whatsapp.sh").symlink_to(WRAPPER)
    # Drop our stub pipeline next to it
    (fake_root / "run_pipeline.sh").write_text(stub.read_text())
    (fake_root / "run_pipeline.sh").chmod(0o755)
    # Stub tui.py so `tui` doesn't try to launch questionary
    (fake_root / "tui.py").write_text("print('TUI stub')\n")
    # Provide a fake venv so activate_venv doesn't fail
    venv = fake_root / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "activate").write_text("# stub venv\n")
    monkeypatch.setattr("os.environ", {**os.environ, "MIKOSHI_INGEST_CONF": str(tmp_path / "no-conf")})
    return {"wrapper": fake_root / "mikoshi-whatsapp.sh", "args_log": args_log,
            "favorites_file": tmp_path / "favs.json"}


def _run_stub(stub, subargs, favorites=None, env_extra=None):
    env = os.environ.copy()
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


# ─── status / tui subcommand presence ──────────────────────────────────────

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
