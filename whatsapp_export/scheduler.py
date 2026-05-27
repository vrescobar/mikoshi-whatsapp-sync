"""LaunchAgent scheduler for the Mikoshi WhatsApp sync.

On macOS, ``launchd`` is the right tool for "run this every day at a
fixed time": it survives sleep, login/logout cycles, and reboots, and
can re-fire a missed run when the laptop wakes up — none of which
``cron`` does well on a personal Mac.

The plist this module manages lives at::

    ~/Library/LaunchAgents/com.mikoshi.sync.plist

It runs ``mikoshi-whatsapp.sh sync`` once per day at a user-picked
hour/minute (local Mac timezone — launchd's ``StartCalendarInterval``
is always local).

Layout (no per-second cron, no every-N-hours pattern — just one daily
slot, the only thing the TUI needs to expose right now):

    {
        Label: "com.mikoshi.sync",
        ProgramArguments: ["/bin/bash", "-lc", '"<abs-path>" sync'],
        StartCalendarInterval: {Hour: 6, Minute: 30},
        StandardOutPath: "<logs>/launchagent.out.log",
        StandardErrorPath: "<logs>/launchagent.err.log",
        RunAtLoad: false,
    }

``-lc`` is important — launchd starts processes with a minimal env, so
without ``-l`` we miss the user's PATH and the keychain access the
backup decrypter relies on.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


LABEL = "com.mikoshi.sync"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SCRIPT_DIR = Path(__file__).parent.resolve()
WRAPPER = SCRIPT_DIR / "mikoshi-whatsapp.sh"
LOG_DIR = SCRIPT_DIR / "logs"
STDOUT_LOG = LOG_DIR / "launchagent.out.log"
STDERR_LOG = LOG_DIR / "launchagent.err.log"


@dataclass
class ScheduleInfo:
    enabled: bool
    hour: int
    minute: int
    plist_path: Path


def current_schedule() -> ScheduleInfo | None:
    """Return the active schedule, or None if no plist is installed.

    Note: a plist on disk doesn't *guarantee* launchd has loaded it —
    that's a separate concern (the user can rm the plist while it's
    still bootstrapped). This function answers "is the file there?"
    which is what the TUI's "Scheduled at 06:00 / not scheduled" line
    needs. ``launchctl print`` would give a more authoritative answer
    but the parse surface is uglier and changes between macOS versions.
    """
    if not PLIST_PATH.exists():
        return None
    try:
        with open(PLIST_PATH, "rb") as f:
            data = plistlib.load(f)
    except (plistlib.InvalidFileException, OSError):
        return None
    interval = data.get("StartCalendarInterval") or {}
    if not isinstance(interval, dict):
        return None
    hour = interval.get("Hour")
    minute = interval.get("Minute", 0)
    if hour is None:
        return None
    return ScheduleInfo(
        enabled=not data.get("Disabled", False),
        hour=int(hour),
        minute=int(minute),
        plist_path=PLIST_PATH,
    )


def install_schedule(hour: int, minute: int) -> Path:
    """Write the plist and bootstrap it via launchctl.

    Replaces any existing entry — call ``disable_schedule`` first if
    you want a clean teardown, otherwise the bootstrap+bootout dance
    below handles the in-place update.

    Returns the path to the installed plist for logging.
    """
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"hour/minute out of range: {hour:02d}:{minute:02d}")
    if not WRAPPER.exists():
        # Misconfigured install would write a plist pointing at a non-
        # existent script, which launchd would load and then keep
        # failing silently. Refuse early.
        raise FileNotFoundError(f"wrapper not found at {WRAPPER}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            "/bin/bash", "-lc",
            f'"{WRAPPER}" sync',
        ],
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "StandardOutPath": str(STDOUT_LOG),
        "StandardErrorPath": str(STDERR_LOG),
        # RunAtLoad=false prevents launchd from firing one immediately
        # when the user reboots — that would surprise them. The scheduled
        # interval is the only trigger.
        "RunAtLoad": False,
    }

    # Atomic write so a partial plist can't be loaded by launchd if
    # we crash mid-rewrite.
    tmp = PLIST_PATH.with_suffix(".plist.tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(plist, f)
    os.replace(tmp, PLIST_PATH)

    # Bootstrap into the user's gui domain so the agent runs without
    # the user having to log out and back in.
    _bootstrap_plist()
    return PLIST_PATH


def disable_schedule() -> bool:
    """Bootout from launchd and remove the plist. Returns True if a
    plist was removed; False if there was nothing to remove."""
    if not PLIST_PATH.exists():
        return False
    _bootout_plist()
    PLIST_PATH.unlink(missing_ok=True)
    return True


def last_run_summary() -> str | None:
    """Return a short one-line summary of the most recent automatic run,
    or None if no log exists yet.

    We sniff the most recent ``logs/cron_*.log`` (the same log files
    the wrapper writes for any sync invocation, not just LaunchAgent
    ones). Sufficient signal: timestamp + exit code if visible.
    """
    if not LOG_DIR.exists():
        return None
    logs = sorted(LOG_DIR.glob("cron_*.log"))
    if not logs:
        return None
    latest = logs[-1]
    try:
        text = latest.read_text(errors="replace").splitlines()
    except OSError:
        return None
    # Look for the wrapper's "sync finished (exit N)" tail line — last
    # line is fine because the wrapper writes it at the very end.
    for line in reversed(text[-50:]):
        if "sync finished" in line:
            return f"{latest.name}: {line.strip()}"
    # Fallback: just point at the log
    return f"{latest.name} (no completion line found yet)"


def _bootstrap_plist() -> None:
    uid = os.getuid()
    # ``bootstrap`` errors out if the label is already loaded, so we
    # bootout first (ignoring failure if it wasn't loaded).
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
        capture_output=True, text=True, check=False,
    )
    res = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        # 5 = "already bootstrapped" on some macOS versions; tolerate.
        if res.returncode != 5:
            raise RuntimeError(
                f"launchctl bootstrap failed (rc={res.returncode}): "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )


def _bootout_plist() -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
        capture_output=True, text=True, check=False,
    )
