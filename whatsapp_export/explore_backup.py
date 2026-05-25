#!/usr/bin/env python3
"""
Explore / re-process an existing iPhone backup without re-downloading.

Use cases:
  - List all WhatsApp chats with message counts (to pick a --contact)
  - Decrypt only ChatStorage.sqlite and inspect interactively
  - Re-run extraction against an existing backup (skipping PHASE 1+2)

Usage:
  # List chats
  python3 explore_backup.py list-chats

  # Decrypt and open ChatStorage.sqlite in sqlite3 REPL
  python3 explore_backup.py shell

  # Re-extract only — backup must exist already
  python3 explore_backup.py extract --mode full-contact --contact "Alice"
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


def decrypt_chatstorage(work_dir: Path) -> Path:
    """Decrypt ChatStorage.sqlite if not already present."""
    chat_db = work_dir / "ChatStorage.sqlite"
    if chat_db.exists():
        return chat_db

    base = get_backup_dir()
    device_backup = find_device_backup(base)
    passphrase = get_passphrase()

    print(f"[INFO] Decrypting ChatStorage.sqlite from {device_backup.name[:12]}...")
    eb = EncryptedBackup(backup_directory=str(device_backup), passphrase=passphrase)
    work_dir.mkdir(parents=True, exist_ok=True)
    eb.extract_file(
        relative_path=RelativePath.WHATSAPP_MESSAGES,
        output_filename=str(chat_db),
    )
    return chat_db


def decrypt_media(work_dir: Path) -> Path:
    """Decrypt the WhatsApp media domain (for full extract)."""
    media_dir = work_dir / "media"
    if media_dir.exists() and any(media_dir.iterdir()):
        return media_dir

    base = get_backup_dir()
    device_backup = find_device_backup(base)
    passphrase = get_passphrase()

    print("[INFO] Decrypting WhatsApp media (this may take a while)...")
    eb = EncryptedBackup(backup_directory=str(device_backup), passphrase=passphrase)
    media_dir.mkdir(parents=True, exist_ok=True)
    eb.extract_files(
        domain_like="AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
        output_folder=str(media_dir),
    )
    return media_dir


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

    print(f"\n{'Last msg':<20} {'Msgs':>7}  {'Type':<6} {'Name':<35} JID")
    print("-" * 110)
    for r in rows:
        last = ""
        if r["last_ts"]:
            last = datetime.fromtimestamp(
                r["last_ts"] + IOS_EPOCH, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
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


def cmd_extract(args):
    """Re-run extraction stage without touching the iPhone."""
    script_dir = Path(__file__).parent
    work_dir = script_dir / "temp" / "extracted"
    chat_db = decrypt_chatstorage(work_dir)
    media_dir = decrypt_media(work_dir)

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_file = script_dir / "exports" / f"whatsapp_export_{timestamp}.json"
    attachments_dir = script_dir / "exports" / "attachments"
    state_file = script_dir / ".sync_state.json"

    cmd = [
        sys.executable, str(script_dir / "extract_messages.py"),
        "--db", str(chat_db),
        "--extracted-root", str(work_dir),
        "--output", str(export_file),
        "--attachments-dir", str(attachments_dir),
        "--state-file", str(state_file),
        "--mode", args.mode,
    ]
    if args.contact:
        cmd += ["--contact", args.contact]
    if args.include_system:
        cmd.append("--include-system")

    print(f"[INFO] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print(f"\n[OK] Export: {export_file}")
        print(f"[INFO] To push to Mikoshi: python3 push_via_api.py --export {export_file}")
    sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-chats", help="List all chats with last-message date and counts")
    sub.add_parser("shell", help="Open ChatStorage.sqlite in sqlite3 interactive shell")

    p_extract = sub.add_parser("extract", help="Re-run extraction without re-downloading the backup")
    p_extract.add_argument("--mode", choices=["incremental", "full", "full-contact"],
                           default="incremental")
    p_extract.add_argument("--contact", help="Required for --mode=full-contact")
    p_extract.add_argument("--include-system", action="store_true")

    args = parser.parse_args()

    if args.cmd == "list-chats":
        cmd_list_chats(args)
    elif args.cmd == "shell":
        cmd_shell(args)
    elif args.cmd == "extract":
        if args.mode == "full-contact" and not args.contact:
            parser.error("--contact required when --mode=full-contact")
        cmd_extract(args)


if __name__ == "__main__":
    main()
