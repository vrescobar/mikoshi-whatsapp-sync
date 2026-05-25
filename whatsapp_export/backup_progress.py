#!/usr/bin/env python3
"""
Run `idevicebackup2 backup` and render two fixed progress bars:
  - Current file:  pct + size of the file being received
  - Session total: rolling sum of bytes transferred + file count

Parses idevicebackup2 stderr/stdout patterns:
  "[==========] 100% (15.3 MB/15.3 MB)"
  "Receiving files"
  "Sending '<name>'"
  "Backup Successful." / "Backup Failed."

Falls back to plain pass-through if `rich` isn't installed.

Usage (same args as idevicebackup2 backup):
  python3 backup_progress.py --udid <UDID> /path/to/backup-dir
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:
    from rich.console import Console
    from rich.progress import (
        Progress, BarColumn, TextColumn, TimeElapsedColumn,
        DownloadColumn, TransferSpeedColumn, SpinnerColumn,
    )
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False


# "[==========] 100% (15.3 MB/15.3 MB)" or "  0% Finished/14.5 MB"
PCT_RE = re.compile(r"(\d{1,3})%\s*(?:Finished)?\s*(?:\(([\d.]+)\s*([KMG]?B)/([\d.]+)\s*([KMG]?B)\))?")
RECEIVING_RE = re.compile(r"^\s*Receiving\b")
DONE_RE = re.compile(r"Backup (Successful|Failed|Complete)", re.IGNORECASE)


def to_bytes(value: float, unit: str) -> int:
    mul = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    return int(value * mul.get(unit.upper(), 1))


def run_plain(cmd: list[str]) -> int:
    """No rich — just stream output through."""
    return subprocess.call(cmd)


def run_with_progress(cmd: list[str]) -> int:
    console = Console()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )

    file_size_seen = {}        # file index -> bytes (de-dup by what we've added)
    files_done = 0
    total_bytes = 0
    current_file_idx = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=10,
    ) as progress:

        # We don't know totals upfront. Set arbitrary high values and update as we go.
        file_task = progress.add_task("Current file", total=None)
        total_task = progress.add_task("Session total", total=None)

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")

                if RECEIVING_RE.search(line):
                    current_file_idx += 1
                    progress.update(file_task, completed=0, total=None,
                                    description=f"File #{current_file_idx}")
                    continue

                m = PCT_RE.search(line)
                if m:
                    pct = int(m.group(1))
                    if m.group(2):  # "(X MB/Y MB)" form
                        cur = to_bytes(float(m.group(2)), m.group(3))
                        tot = to_bytes(float(m.group(4)), m.group(5))
                        progress.update(file_task, completed=cur, total=tot)

                        # When the file finishes, fold into session total once
                        if pct == 100:
                            already = file_size_seen.get(current_file_idx, 0)
                            delta = tot - already
                            if delta > 0:
                                total_bytes += delta
                                file_size_seen[current_file_idx] = tot
                            files_done += 1
                            progress.update(
                                total_task,
                                completed=total_bytes,
                                total=total_bytes,  # grows as we discover more files
                                description=f"Session total — {files_done} files",
                            )
                    continue

                if DONE_RE.search(line):
                    progress.console.print(f"[bold green]{line}[/]")
                    continue

                # Non-progress noise (errors, status text) — print above bars
                if line.strip() and not line.strip().startswith("["):
                    progress.console.print(line)

        except KeyboardInterrupt:
            proc.terminate()
            raise

    return proc.wait()


def main():
    cmd = ["idevicebackup2", "backup"] + sys.argv[1:]
    if not HAVE_RICH or os.environ.get("NO_PROGRESS"):
        sys.exit(run_plain(cmd))
    sys.exit(run_with_progress(cmd))


if __name__ == "__main__":
    main()
