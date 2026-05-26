#!/usr/bin/env python3
"""
Verify the integrity of an iPhone encrypted backup before running the pipeline.

Four progressive checks, each reusable in isolation:

  1. Structure       — Manifest.plist / Manifest.db / Status.plist / Info.plist
                       exist and have valid magic bytes (no NULs, right header).
  2. Status          — Status.plist parses; BackupState=='new', SnapshotState=='finished'.
  3. Keybag          — passphrase from Keychain unlocks the backup's keybag,
                       Manifest.db decrypts. Validates the crypto layer end-to-end
                       without extracting any data.
  4. ChatStorage     — extracts only ChatStorage.sqlite to a temp file, opens it,
                       counts chats + messages, prints the latest one, drops the temp.

Each level depends on the previous one. The default runs all four.

Usage:
  python3 verify_backup.py                    # auto-discover via MIKOSHI_BACKUP_DIR
  python3 verify_backup.py --level 3          # stop after keybag check
  python3 verify_backup.py --backup-dir /Volumes/X/iphone_backup
  python3 verify_backup.py --json             # machine-readable output

Exit codes:
  0  — all selected checks passed
  1  — at least one check failed
  2  — environment problem (no backup found, no passphrase, missing deps)
"""

import argparse
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False

# iphone_backup_decrypt is required for levels 3-4; we import lazily so
# levels 1-2 still work on a bare install.


REQUIRED_FILES = ("Manifest.plist", "Manifest.db", "Status.plist", "Info.plist")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


# ─── discovery ─────────────────────────────────────────────────────────────

def discover_backup_dir(explicit: Optional[Path] = None) -> Path:
    """Locate the UDID dir under $MIKOSHI_BACKUP_DIR/backup/<UDID>/."""
    if explicit:
        # Accept either the parent (auto-find UDID) or the UDID dir itself
        if (explicit / "Manifest.plist").exists() or (explicit / "Status.plist").exists():
            return explicit
        if (explicit / "backup").is_dir():
            explicit = explicit / "backup"

    base = explicit
    if base is None:
        env = os.environ.get("MIKOSHI_BACKUP_DIR")
        if not env:
            raise SystemExit(
                "ERROR: no backup dir given. Set MIKOSHI_BACKUP_DIR or use --backup-dir."
            )
        base = Path(env) / "backup"

    if not base.is_dir():
        raise SystemExit(f"ERROR: backup root not found: {base}")

    udid_dirs = [d for d in base.iterdir() if d.is_dir() and len(d.name) > 20]
    if not udid_dirs:
        raise SystemExit(f"ERROR: no UDID-named subdirectory under {base}")
    if len(udid_dirs) > 1:
        # Pick the most recently modified
        udid_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return udid_dirs[0]


def get_passphrase() -> str:
    res = subprocess.run(
        ["security", "find-generic-password",
         "-a", "iphone_backup", "-s", "iphone_backup_password", "-w"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise SystemExit(
            "ERROR: backup password not in Keychain. "
            "Run: security add-generic-password -a iphone_backup "
            "-s iphone_backup_password -w 'YOUR_PASSWORD'"
        )
    return res.stdout.strip()


# ─── individual checks ─────────────────────────────────────────────────────

PLIST_MAGICS = (b"bplist", b"<?xml")
SQLITE_MAGIC = b"SQLite format 3"


def check_structure(udid_dir: Path) -> CheckResult:
    """Level 1: required files present, non-zero, with the right magic bytes."""
    missing = []
    truncated = []
    bad_magic = []
    file_info = {}

    for fname in REQUIRED_FILES:
        p = udid_dir / fname
        if not p.exists():
            missing.append(fname)
            continue
        size = p.stat().st_size
        if size == 0:
            truncated.append(fname)
            continue

        with p.open("rb") as f:
            head = f.read(16)

        is_plist = fname.endswith(".plist")
        is_db = fname == "Manifest.db"

        if is_plist and not any(head.startswith(m) for m in PLIST_MAGICS):
            bad_magic.append((fname, head[:8].hex()))
        elif is_db and not head.startswith(SQLITE_MAGIC):
            bad_magic.append((fname, head[:16].hex()))

        file_info[fname] = {"size": size, "head_hex": head[:8].hex()}

    if missing or truncated or bad_magic:
        parts = []
        if missing:
            parts.append(f"missing: {', '.join(missing)}")
        if truncated:
            parts.append(f"empty: {', '.join(truncated)}")
        if bad_magic:
            parts.append("bad magic: " + ", ".join(f"{n} ({h})" for n, h in bad_magic))
        return CheckResult("structure", False, "; ".join(parts), {"files": file_info})

    return CheckResult(
        "structure", True,
        f"all 4 metadata files present, magic bytes OK",
        {"files": file_info},
    )


def check_status_plist(udid_dir: Path) -> CheckResult:
    """Level 2: Status.plist is valid + reports a finished, completed backup."""
    p = udid_dir / "Status.plist"
    try:
        with p.open("rb") as f:
            data = plistlib.load(f)
    except Exception as e:
        return CheckResult("status_plist", False, f"unparseable: {e}")

    state = data.get("BackupState")
    snapshot = data.get("SnapshotState")
    is_full = data.get("IsFullBackup")

    problems = []
    if state != "new":
        problems.append(f"BackupState={state!r} (expected 'new')")
    if snapshot != "finished":
        problems.append(f"SnapshotState={snapshot!r} (expected 'finished')")

    if problems:
        return CheckResult("status_plist", False, "; ".join(problems),
                           {"BackupState": state, "SnapshotState": snapshot,
                            "IsFullBackup": is_full})

    return CheckResult(
        "status_plist", True,
        f"BackupState={state}, SnapshotState={snapshot}, IsFullBackup={is_full}",
        {"BackupState": state, "SnapshotState": snapshot, "IsFullBackup": is_full},
    )


def check_keybag(udid_dir: Path, passphrase: str) -> CheckResult:
    """Level 3: passphrase unlocks the keybag, Manifest.db decrypts."""
    try:
        from iphone_backup_decrypt import EncryptedBackup
    except ImportError:
        return CheckResult("keybag", False,
                           "iphone_backup_decrypt not installed (pip install in .venv)")

    try:
        eb = EncryptedBackup(backup_directory=str(udid_dir), passphrase=passphrase)
        eb._read_and_unlock_keybag()
        eb._decrypt_manifest_db_file()
    except plistlib.InvalidFileException:
        return CheckResult("keybag", False, "Manifest.plist is corrupt (plistlib rejected it)")
    except ValueError as e:
        msg = str(e).lower()
        if any(k in msg for k in ("password", "passphrase", "keybag", "decrypt")):
            return CheckResult("keybag", False, f"crypto rejected: {e}")
        raise
    except Exception as e:
        return CheckResult("keybag", False, f"unexpected: {type(e).__name__}: {e}")

    return CheckResult("keybag", True, "keybag unlocks, Manifest.db decrypts")


def check_chatstorage(udid_dir: Path, passphrase: str) -> CheckResult:
    """Level 4: ChatStorage.sqlite extracts + opens + has plausible content."""
    try:
        from iphone_backup_decrypt import EncryptedBackup, RelativePath
    except ImportError:
        return CheckResult("chatstorage", False, "iphone_backup_decrypt not installed")

    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        eb = EncryptedBackup(backup_directory=str(udid_dir), passphrase=passphrase)
        eb.extract_file(
            relative_path=RelativePath.WHATSAPP_MESSAGES,
            output_filename=str(tmp_path),
        )

        if tmp_path.stat().st_size == 0:
            return CheckResult("chatstorage", False, "ChatStorage extracted but empty")

        conn = sqlite3.connect(tmp_path)
        chats = conn.execute(
            "SELECT count(*) FROM ZWACHATSESSION WHERE ZCONTACTJID IS NOT NULL"
        ).fetchone()[0]
        msgs = conn.execute("SELECT count(*) FROM ZWAMESSAGE").fetchone()[0]

        latest = conn.execute(
            "SELECT datetime(ZMESSAGEDATE + 978307200, 'unixepoch'), ZTEXT "
            "FROM ZWAMESSAGE WHERE ZTEXT IS NOT NULL "
            "ORDER BY ZMESSAGEDATE DESC LIMIT 1"
        ).fetchone()

        # Schema introspection — solves the ZMEDIASIZE vs ZFILESIZE question
        media_cols = [
            r[1] for r in conn.execute("PRAGMA table_info(ZWAMEDIAITEM)").fetchall()
        ]
        size_cols = [c for c in media_cols
                     if "size" in c.lower() or "file" in c.lower()]

        conn.close()

        return CheckResult(
            "chatstorage", True,
            f"{chats} chats, {msgs:,} messages",
            {
                "chats": chats,
                "messages": msgs,
                "latest_ts": latest[0] if latest else None,
                "latest_preview": (latest[1] or "")[:80] if latest else None,
                "media_size_columns": size_cols,
                "all_media_columns": media_cols,
            },
        )
    except plistlib.InvalidFileException:
        return CheckResult("chatstorage", False, "Manifest.plist corrupt")
    except Exception as e:
        return CheckResult("chatstorage", False, f"{type(e).__name__}: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── runner ────────────────────────────────────────────────────────────────

CHECKS = [
    ("structure", check_structure),
    ("status_plist", check_status_plist),
    ("keybag", check_keybag),
    ("chatstorage", check_chatstorage),
]


def run_checks(udid_dir: Path, max_level: int) -> list[CheckResult]:
    results: list[CheckResult] = []
    passphrase = None

    for i, (name, fn) in enumerate(CHECKS, start=1):
        if i > max_level:
            break

        # Levels 3+ need the passphrase
        if i >= 3 and passphrase is None:
            try:
                passphrase = get_passphrase()
            except SystemExit as e:
                results.append(CheckResult(name, False, str(e)))
                break

        if i == 1:
            results.append(fn(udid_dir))
        elif i == 2:
            results.append(fn(udid_dir))
        else:
            results.append(fn(udid_dir, passphrase))

        # Bail early if a check failed — the next one would fail too and
        # the error would be less actionable.
        if not results[-1].passed:
            break

    return results


def render_table(results: list[CheckResult]) -> None:
    if HAVE_RICH:
        console = Console()
        table = Table(title="Backup verification", header_style="bold cyan")
        table.add_column("#", justify="right")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        for i, r in enumerate(results, start=1):
            status = "[green]✓ PASS[/]" if r.passed else "[red]✗ FAIL[/]"
            table.add_row(str(i), r.name, status, r.detail)
        console.print(table)

        # Pretty-print the chatstorage extras
        for r in results:
            if r.name == "chatstorage" and r.passed and r.extra:
                e = r.extra
                console.print()
                console.print(f"  Latest message: [dim]{e.get('latest_ts')}[/] — {e.get('latest_preview')!r}")
                console.print(f"  ZWAMEDIAITEM size-ish columns: {e.get('media_size_columns')}")
    else:
        for i, r in enumerate(results, start=1):
            marker = "✓" if r.passed else "✗"
            print(f"{i}. [{marker}] {r.name:<14} {r.detail}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backup-dir", type=Path,
                        help="Override $MIKOSHI_BACKUP_DIR. May be the parent or the UDID dir.")
    parser.add_argument("--level", type=int, default=4, choices=[1, 2, 3, 4],
                        help="Run checks 1..N (default: 4 = all).")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output.")
    args = parser.parse_args()

    try:
        udid_dir = discover_backup_dir(args.backup_dir)
    except SystemExit as e:
        print(e, file=sys.stderr)
        sys.exit(2)

    if not args.json:
        print(f"Verifying: {udid_dir}\n")

    results = run_checks(udid_dir, args.level)

    if args.json:
        out = {
            "backup_dir": str(udid_dir),
            "max_level_requested": args.level,
            "checks_run": len(results),
            "all_passed": all(r.passed for r in results),
            "results": [
                {"name": r.name, "passed": r.passed,
                 "detail": r.detail, "extra": r.extra}
                for r in results
            ],
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        render_table(results)

    sys.exit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
