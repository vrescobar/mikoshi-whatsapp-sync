#!/usr/bin/env python3
"""
Explore an existing iPhone backup without re-downloading.

Use cases:
  - List all WhatsApp chats with message counts
  - Decrypt only ChatStorage.sqlite and inspect interactively in sqlite3

Usage:
  # List chats
  python3 explore_backup.py list-chats

  # Decrypt and open ChatStorage.sqlite in sqlite3 REPL
  python3 explore_backup.py shell

Note:
  The `extract` subcommand has been removed — use
  `./mikoshi-whatsapp.sh sync --from-phase 4` (or `--from-phase 3` if you
  also need to re-decrypt) for the same effect. That path supports
  favorites, --chat-jid, --since, --skip-remote-sync, and feeds the new
  cursor-cache model. See REDESIGN.md §7.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    from iphone_backup_decrypt import EncryptedBackup, RelativePath
except ImportError:
    print("[ERROR] Activate venv first: source .venv/bin/activate", file=sys.stderr)
    sys.exit(1)


def get_backup_dir() -> Path:
    base = os.environ.get("MIKOSHI_BACKUP_DIR")
    if not base:
        print("[ERROR] MIKOSHI_BACKUP_DIR not set. Either:", file=sys.stderr)
        print("  export MIKOSHI_BACKUP_DIR=/Volumes/models/iPhoneBackup", file=sys.stderr)
        print("  or put it in ~/.mikoshi-ingest.conf", file=sys.stderr)
        sys.exit(1)
    return Path(base)


def get_passphrase() -> str:
    result = subprocess.run(
        ["security", "find-generic-password",
         "-a", "iphone_backup", "-s", "iphone_backup_password", "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("[ERROR] Backup password not in Keychain.", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def find_device_backup(base: Path) -> Path:
    """Locate the UDID dir inside MIKOSHI_BACKUP_DIR/backup/."""
    backup_root = base / "backup"
    if not backup_root.is_dir():
        print(f"[ERROR] No backup found at {backup_root}", file=sys.stderr)
        sys.exit(1)
    udid_dirs = [d for d in backup_root.iterdir() if d.is_dir() and len(d.name) > 20]
    if not udid_dirs:
        print(f"[ERROR] No device backup found in {backup_root}", file=sys.stderr)
        sys.exit(1)
    return udid_dirs[0]


# Files every encrypted iOS backup must have. Used as a pre-flight check
# so we give a clear error instead of a plistlib stacktrace when the
# backup is mid-flight or got corrupted.
REQUIRED_BACKUP_FILES = ("Manifest.plist", "Manifest.db", "Info.plist")


def validate_backup_structure(device_backup: Path) -> None:
    """Raise SystemExit with an actionable message if the backup is incomplete."""
    missing = []
    truncated = []
    for fname in REQUIRED_BACKUP_FILES:
        p = device_backup / fname
        if not p.exists():
            missing.append(fname)
        elif p.stat().st_size == 0:
            truncated.append(fname)

    if missing or truncated:
        print(f"[ERROR] Backup at {device_backup} looks incomplete:", file=sys.stderr)
        for f in missing:
            print(f"  • missing: {f}", file=sys.stderr)
        for f in truncated:
            print(f"  • empty:   {f}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Likely causes:", file=sys.stderr)
        print("  - A previous backup was interrupted before completing.", file=sys.stderr)
        print("  - The backup directory got wiped or partially deleted.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Fix: run the full pipeline to recreate the backup:", file=sys.stderr)
        print("  ./mikoshi-whatsapp.sh sync --all", file=sys.stderr)
        sys.exit(3)


def _safe_decrypt(operation_label: str, fn):
    """Run a decryption block, mapping cryptic errors to friendly messages."""
    import plistlib
    try:
        return fn()
    except plistlib.InvalidFileException:
        print(f"[ERROR] {operation_label}: Manifest.plist is corrupted or truncated.",
              file=sys.stderr)
        print("Most common reason: the backup was interrupted mid-flight.", file=sys.stderr)
        print("Fix: ./mikoshi-whatsapp.sh sync --all", file=sys.stderr)
        sys.exit(3)
    except ValueError as e:
        msg = str(e).lower()
        if "password" in msg or "passphrase" in msg or "decrypt" in msg or "keybag" in msg:
            print(f"[ERROR] {operation_label}: backup password is wrong.", file=sys.stderr)
            print("The password in Keychain doesn't match the one used to encrypt this backup.",
                  file=sys.stderr)
            print("Fix:", file=sys.stderr)
            print("  security delete-generic-password -a iphone_backup -s iphone_backup_password",
                  file=sys.stderr)
            print("  security add-generic-password -a iphone_backup -s iphone_backup_password "
                  "-w 'CORRECT_PASSWORD'", file=sys.stderr)
            sys.exit(3)
        raise
    except FileNotFoundError as e:
        print(f"[ERROR] {operation_label}: missing file — {e}", file=sys.stderr)
        sys.exit(3)


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _looks_like_sqlite(path: Path) -> bool:
    """A killed decrypt run leaves the output file size-extended but with
    a zero (or garbage) header. `size > 0` passes, but Phase 4 then dies
    with `sqlite3.DatabaseError: file is not a database`. Header check
    catches that footgun without opening the DB."""
    try:
        with path.open("rb") as f:
            return f.read(16) == _SQLITE_MAGIC
    except OSError:
        return False


def decrypt_chatstorage(work_dir: Path) -> Path:
    """Decrypt ChatStorage.sqlite if not already present and valid."""
    chat_db = work_dir / "ChatStorage.sqlite"
    if chat_db.exists() and _looks_like_sqlite(chat_db):
        return chat_db
    if chat_db.exists():
        # File present but header looks wrong — almost certainly a truncated
        # write from a killed decrypt. Drop it so we re-extract cleanly.
        print(f"[WARN] {chat_db} has an invalid SQLite header — re-extracting.")
        chat_db.unlink()

    base = get_backup_dir()
    device_backup = find_device_backup(base)
    validate_backup_structure(device_backup)
    passphrase = get_passphrase()

    print(f"[INFO] Decrypting ChatStorage.sqlite from {device_backup.name[:12]}...")
    work_dir.mkdir(parents=True, exist_ok=True)

    def _do():
        eb = EncryptedBackup(backup_directory=str(device_backup), passphrase=passphrase)
        eb.extract_file(
            relative_path=RelativePath.WHATSAPP_MESSAGES,
            output_filename=str(chat_db),
        )
    _safe_decrypt("decrypting ChatStorage", _do)
    return chat_db


def cmd_list_chats(args):
    import sqlite3
    from datetime import datetime, timezone

    script_dir = Path(__file__).parent
    work_dir = script_dir / "temp" / "extracted"
    chat_db = decrypt_chatstorage(work_dir)

    IOS_EPOCH = 978307200
    conn = sqlite3.connect(chat_db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            s.ZCONTACTJID as jid,
            s.ZPARTNERNAME as name,
            s.ZLASTMESSAGEDATE as last_ts,
            COUNT(m.Z_PK) as msg_count
        FROM ZWACHATSESSION s
        LEFT JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
        WHERE s.ZCONTACTJID IS NOT NULL
        GROUP BY s.Z_PK
        ORDER BY s.ZLASTMESSAGEDATE DESC NULLS LAST
    """).fetchall()

    def _fmt_ts(ios_ts):
        # Real ChatStorage rows occasionally carry garbage values (year 11001
        # from uninitialised system events / corrupted entries). Don't crash
        # the whole listing because of one bad row.
        if not ios_ts:
            return ""
        try:
            unix = ios_ts + IOS_EPOCH
            if not 0 <= unix <= 4_102_444_800:  # 1970 .. 2100
                return "—"
            return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError, OSError):
            return "—"

    print(f"\n{'Last msg':<20} {'Msgs':>7}  {'Type':<6} {'Name':<35} JID")
    print("-" * 110)
    for r in rows:
        last = _fmt_ts(r["last_ts"])
        kind = "group" if (r["jid"] or "").endswith("@g.us") else "1-on-1"
        name = (r["name"] or "—")[:34]
        print(f"{last:<20} {r['msg_count']:>7}  {kind:<6} {name:<35} {r['jid']}")
    print(f"\nTotal: {len(rows)} chats")


def cmd_shell(args):
    script_dir = Path(__file__).parent
    work_dir = script_dir / "temp" / "extracted"
    chat_db = decrypt_chatstorage(work_dir)
    print(f"[INFO] Opening {chat_db}")
    print("[INFO] Useful tables: ZWACHATSESSION, ZWAMESSAGE, ZWAMEDIAITEM, ZWAGROUPMEMBER")
    print("[INFO] Type .schema ZWAMESSAGE   to see fields")
    print("[INFO] Type .quit                to exit\n")
    os.execvp("sqlite3", ["sqlite3", str(chat_db)])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-chats", help="List all chats with last-message date and counts")
    sub.add_parser("shell", help="Open ChatStorage.sqlite in sqlite3 interactive shell")

    args = parser.parse_args()

    if args.cmd == "list-chats":
        cmd_list_chats(args)
    elif args.cmd == "shell":
        cmd_shell(args)


if __name__ == "__main__":
    main()
