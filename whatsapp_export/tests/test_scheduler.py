"""Tests for the LaunchAgent scheduler module.

The module shells out to ``launchctl`` for bootstrap/bootout. Those
subprocess calls are mocked here so the test suite doesn't tamper with
the developer's real LaunchAgent registry; the plist file write is
real (against tmp_path).
"""
import os
import plistlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scheduler  # noqa: E402


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    """Redirect PLIST_PATH and LOG_DIR onto tmp_path so install_schedule
    writes into the test sandbox instead of ~/Library/LaunchAgents."""
    monkeypatch.setattr(scheduler, "PLIST_PATH", tmp_path / "com.mikoshi.sync.plist")
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(scheduler, "STDOUT_LOG", tmp_path / "logs" / "out.log")
    monkeypatch.setattr(scheduler, "STDERR_LOG", tmp_path / "logs" / "err.log")
    # WRAPPER must point at a real file for install_schedule to accept it
    wrapper = tmp_path / "mikoshi-whatsapp.sh"
    wrapper.write_text("#!/bin/bash\necho stub\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(scheduler, "WRAPPER", wrapper)
    return {"tmp": tmp_path, "wrapper": wrapper}


@pytest.fixture
def mocked_launchctl(monkeypatch):
    """Catch every subprocess.run call (the launchctl ones) so the test
    can verify what verbs the scheduler invoked without actually loading
    or removing real launchd entries."""
    calls = []

    class _FakeResult:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _FakeResult(rc=0)

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    return calls


# ─── current_schedule ──────────────────────────────────────────────────────


class TestCurrentSchedule:
    def test_returns_none_when_no_plist(self, patched_paths):
        assert scheduler.current_schedule() is None

    def test_parses_existing_plist(self, patched_paths):
        plist_path = patched_paths["tmp"] / "com.mikoshi.sync.plist"
        with open(plist_path, "wb") as f:
            plistlib.dump({
                "Label": "com.mikoshi.sync",
                "ProgramArguments": ["/bin/bash", "-lc", "stub sync"],
                "StartCalendarInterval": {"Hour": 7, "Minute": 30},
            }, f)
        info = scheduler.current_schedule()
        assert info is not None
        assert info.hour == 7
        assert info.minute == 30
        assert info.enabled is True

    def test_disabled_flag_is_reflected(self, patched_paths):
        plist_path = patched_paths["tmp"] / "com.mikoshi.sync.plist"
        with open(plist_path, "wb") as f:
            plistlib.dump({
                "Label": "com.mikoshi.sync",
                "StartCalendarInterval": {"Hour": 7, "Minute": 30},
                "Disabled": True,
            }, f)
        info = scheduler.current_schedule()
        assert info is not None
        assert info.enabled is False

    def test_corrupted_plist_returns_none(self, patched_paths):
        plist_path = patched_paths["tmp"] / "com.mikoshi.sync.plist"
        plist_path.write_bytes(b"not a plist")
        assert scheduler.current_schedule() is None


# ─── install_schedule ──────────────────────────────────────────────────────


class TestInstallSchedule:
    def test_writes_plist_with_calendar_interval(self, patched_paths, mocked_launchctl):
        path = scheduler.install_schedule(6, 45)
        with open(path, "rb") as f:
            data = plistlib.load(f)
        assert data["Label"] == "com.mikoshi.sync"
        assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 45}
        # ProgramArguments must reference the absolute wrapper path so
        # changing the user's PWD can never break the agent.
        assert "/bin/bash" in data["ProgramArguments"][0]
        assert str(patched_paths["wrapper"]) in data["ProgramArguments"][-1]
        # RunAtLoad=false so a reboot doesn't fire an immediate sync.
        assert data["RunAtLoad"] is False

    def test_uses_login_shell_for_keychain_access(self, patched_paths, mocked_launchctl):
        scheduler.install_schedule(6, 0)
        with open(patched_paths["tmp"] / "com.mikoshi.sync.plist", "rb") as f:
            data = plistlib.load(f)
        # -l flag means login shell, which sources the user's PATH and
        # gives the iPhone-backup decrypter access to Keychain.
        assert "-lc" in data["ProgramArguments"]

    def test_invokes_launchctl_bootstrap(self, patched_paths, mocked_launchctl):
        scheduler.install_schedule(6, 0)
        verbs = [(c[0], c[1]) for c in mocked_launchctl if len(c) >= 2]
        # bootout (to clear any previous load), then bootstrap (to load fresh)
        assert any(v == ("launchctl", "bootout") for v in verbs)
        assert any(v == ("launchctl", "bootstrap") for v in verbs)

    def test_creates_log_directory(self, patched_paths, mocked_launchctl):
        log_dir = patched_paths["tmp"] / "logs"
        assert not log_dir.exists()
        scheduler.install_schedule(6, 0)
        assert log_dir.is_dir()

    def test_rejects_invalid_hour(self, patched_paths, mocked_launchctl):
        with pytest.raises(ValueError):
            scheduler.install_schedule(24, 0)
        with pytest.raises(ValueError):
            scheduler.install_schedule(-1, 0)

    def test_rejects_invalid_minute(self, patched_paths, mocked_launchctl):
        with pytest.raises(ValueError):
            scheduler.install_schedule(6, 60)

    def test_refuses_when_wrapper_missing(self, patched_paths, mocked_launchctl, monkeypatch):
        monkeypatch.setattr(scheduler, "WRAPPER", patched_paths["tmp"] / "does-not-exist.sh")
        with pytest.raises(FileNotFoundError):
            scheduler.install_schedule(6, 0)

    def test_install_then_read_round_trip(self, patched_paths, mocked_launchctl):
        scheduler.install_schedule(9, 15)
        info = scheduler.current_schedule()
        assert info is not None
        assert (info.hour, info.minute) == (9, 15)


# ─── disable_schedule ──────────────────────────────────────────────────────


class TestDisableSchedule:
    def test_removes_plist(self, patched_paths, mocked_launchctl):
        scheduler.install_schedule(6, 0)
        assert (patched_paths["tmp"] / "com.mikoshi.sync.plist").exists()
        assert scheduler.disable_schedule() is True
        assert not (patched_paths["tmp"] / "com.mikoshi.sync.plist").exists()

    def test_idempotent_when_not_installed(self, patched_paths, mocked_launchctl):
        assert scheduler.disable_schedule() is False

    def test_invokes_bootout(self, patched_paths, mocked_launchctl):
        scheduler.install_schedule(6, 0)
        mocked_launchctl.clear()
        scheduler.disable_schedule()
        assert any(c[:2] == ["launchctl", "bootout"] for c in mocked_launchctl)


# ─── last_run_summary ──────────────────────────────────────────────────────


class TestLastRunSummary:
    def test_returns_none_when_no_logs_dir(self, patched_paths):
        assert scheduler.last_run_summary() is None

    def test_returns_none_when_no_logs(self, patched_paths):
        (patched_paths["tmp"] / "logs").mkdir()
        assert scheduler.last_run_summary() is None

    def test_finds_finished_line(self, patched_paths):
        log_dir = patched_paths["tmp"] / "logs"
        log_dir.mkdir()
        log = log_dir / "cron_20260528_063000.log"
        log.write_text(
            "[mikoshi] starting sync\n"
            "[various pipeline output]\n"
            "[mikoshi] 2026-05-28 06:31:42 sync finished (exit 0)\n"
        )
        out = scheduler.last_run_summary()
        assert "cron_20260528_063000.log" in out
        assert "sync finished" in out
        assert "exit 0" in out

    def test_picks_most_recent_when_multiple(self, patched_paths):
        log_dir = patched_paths["tmp"] / "logs"
        log_dir.mkdir()
        old = log_dir / "cron_20260101_060000.log"
        new = log_dir / "cron_20260528_060000.log"
        old.write_text("[mikoshi] sync finished (exit 0)\n")
        new.write_text("[mikoshi] sync finished (exit 3)\n")
        out = scheduler.last_run_summary()
        assert "20260528" in out
        assert "exit 3" in out


# ─── TUI action wiring ─────────────────────────────────────────────────────


class TestScheduleActionRegistered:
    def test_action_schedule_callable_from_dispatch(self):
        import tui
        assert "schedule" in tui._ACTION_DISPATCH
        assert callable(tui._ACTION_DISPATCH["schedule"])

    def test_action_label_discoverable(self):
        import tui
        labels = [label for label, _ in tui.ACTIONS]
        assert any("Schedule" in label or "schedule" in label for label in labels), \
            "Schedule entry should be discoverable in the top-level menu"
