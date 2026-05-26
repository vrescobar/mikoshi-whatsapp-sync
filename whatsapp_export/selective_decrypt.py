#!/usr/bin/env python3
"""
Selective WhatsApp decryption — Phase 3 helpers.

Two strategies share a single entry point:

  decrypt_whatsapp(backup_dir, password, out_dir, chat_jid=None) -> Stats

* Without `chat_jid` ("3A"): decrypt every file under the WhatsApp shared
  domain. This is what `run_pipeline.sh:decrypt_backup()` has always done —
  preserved here so the pipeline can call this module unconditionally.

* With `chat_jid` ("3B"): decrypt ChatStorage.sqlite first, query it for the
  ZMEDIALOCALPATHs belonging to that chat, then decrypt **only** those media
  files (plus the SQLite itself). For a single-chat sync this turns Phase 3
  from "extract tens of GB" into "extract ~1 GB DB + maybe a few hundred MB
  of that chat's media".

The module is pure (no side-effects at import time) and accepts paths as
arguments — so `run_pipeline.sh` can call it via a tiny `python3 -m
selective_decrypt ...`-style invocation, and the unit tests can import the
functions directly with fake EncryptedBackup objects.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional


WHATSAPP_SHARED_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
WHATSAPP_APP_DOMAIN = "AppDomain-net.whatsapp.WhatsApp"

# ChatStorage.sqlite lives in the shared group container at this relpath.
# Hardcoded because RelativePath.WHATSAPP_MESSAGES is the same constant and
# we want this module testable without importing iphone_backup_decrypt.
CHATSTORAGE_RELPATH = "ChatStorage.sqlite"


@dataclass
class DecryptStats:
    """Returned by every public entry point so the caller can log or assert."""

    chat_jid: Optional[str] = None
    chatstorage_extracted: bool = False
    media_decrypted: int = 0
    media_skipped_cached: int = 0
    media_total_candidates: int = 0
    media_relpaths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _open_encrypted_backup(backup_dir: Path, password: str):
    """
    Late import — keeps the module importable for unit tests without the
    iphone_backup_decrypt dependency installed.
    """
    from iphone_backup_decrypt import EncryptedBackup  # type: ignore

    return EncryptedBackup(backup_directory=str(backup_dir), passphrase=password)


def extract_chatstorage(eb, out_dir: Path) -> Path:
    """
    Decrypt ChatStorage.sqlite alone into `out_dir`. Returns its path.
    Idempotent: if the file already exists we still re-extract (cheap, ~1s
    for the metadata DB) to be safe against partial writes from a killed run.
    """
    from iphone_backup_decrypt import RelativePath  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "ChatStorage.sqlite"
    eb.extract_file(
        relative_path=RelativePath.WHATSAPP_MESSAGES,
        output_filename=str(dest),
    )
    return dest


def list_chat_media_relpaths(chatstorage_path: Path, chat_jid: str) -> list[str]:
    """
    Open the (already-decrypted) ChatStorage.sqlite and return the list of
    ZMEDIALOCALPATH values belonging to messages of the chat identified by
    `chat_jid`. NULL paths (text-only messages) are filtered out.

    Returns an empty list if the chat exists but has no media; raises
    `ValueError` if the JID doesn't match any chat (so callers can surface
    a clear error instead of silently decrypting nothing).
    """
    conn = sqlite3.connect(str(chatstorage_path))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        chat_row = cur.execute(
            "SELECT Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID = ?",
            (chat_jid,),
        ).fetchone()
        if not chat_row:
            available = cur.execute(
                "SELECT ZCONTACTJID FROM ZWACHATSESSION "
                "WHERE ZCONTACTJID IS NOT NULL "
                "ORDER BY ZLASTMESSAGEDATE DESC LIMIT 20"
            ).fetchall()
            hint = ", ".join(r["ZCONTACTJID"] for r in available)
            raise ValueError(
                f"No chat with ZCONTACTJID={chat_jid!r}. "
                f"Recent JIDs in this backup: {hint}"
            )
        chat_pk = chat_row["Z_PK"]
        rows = cur.execute(
            """
            SELECT mi.ZMEDIALOCALPATH AS rel
              FROM ZWAMEDIAITEM mi
              JOIN ZWAMESSAGE m ON m.Z_PK = mi.ZMESSAGE
             WHERE m.ZCHATSESSION = ?
               AND mi.ZMEDIALOCALPATH IS NOT NULL
            """,
            (chat_pk,),
        ).fetchall()
        return [r["rel"] for r in rows if r["rel"]]
    finally:
        conn.close()


def decrypt_media_for_relpaths(
    eb,
    relpaths: Iterable[str],
    out_dir: Path,
    *,
    incremental: bool = True,
    domain_like: str = WHATSAPP_SHARED_DOMAIN,
    progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[int, int, list[str]]:
    """
    Decrypt the subset of files in `domain_like` whose Manifest.db
    `relativePath` matches one of the entries in `relpaths`.

    Implementation note: we don't call `extract_file` per relpath — that would
    issue one Manifest.db query per file. Instead we use `extract_files` with
    a `filter_callback` so the library walks the manifest once and asks us
    whether each candidate should be decrypted. `incremental=True` then skips
    files already on disk with an older-or-equal mtime.

    Returns (decrypted_count, skipped_cached_count, missing_relpaths).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(relpaths)
    seen: set[str] = set()
    skipped = 0
    decrypted = 0

    def cb(*, n, total_files, relative_path, domain, file_id, **kwargs):
        nonlocal skipped, decrypted
        if relative_path not in wanted:
            return False
        seen.add(relative_path)
        if progress:
            progress(len(seen), len(wanted))
        # We can't tell from the callback whether `incremental` will skip
        # the file; instead we infer below by comparing return value of
        # extract_files vs the count of True returns.
        decrypted += 1
        return True

    # NB: extract_files needs at least one of relative_paths_like / domain_like.
    # Using domain_like keeps the candidate set bounded to WhatsApp shared.
    actually_written = eb.extract_files(
        domain_like=domain_like,
        output_folder=str(out_dir),
        preserve_folders=True,
        incremental=incremental,
        filter_callback=cb,
    )
    # Files we said "True" for but that incremental decided were already
    # fresh on disk → skipped_cached.
    if actually_written < decrypted:
        skipped = decrypted - actually_written
        decrypted = actually_written

    missing = sorted(wanted - seen)
    return decrypted, skipped, missing


def decrypt_db_only(
    backup_dir: Path,
    password: str,
    out_dir: Path,
    *,
    eb_factory: Optional[Callable[[Path, str], object]] = None,
) -> Path:
    """
    Decrypt only ChatStorage.sqlite — no media. ~10s on a 96 GB backup.

    Used by the redesigned pipeline's Phase 3 (decrypt-db). The plan
    phase reads this DB to count work-to-do; only then does Phase 5
    (materialize) decrypt the media files we actually need.
    """
    factory = eb_factory or _open_encrypted_backup
    eb = factory(backup_dir, password)
    return extract_chatstorage(eb, out_dir)


def decrypt_media_for_jids(
    backup_dir: Path,
    password: str,
    out_dir: Path,
    chatstorage_path: Path,
    jids: Iterable[str],
    *,
    incremental: bool = True,
    eb_factory: Optional[Callable[[Path, str], object]] = None,
) -> DecryptStats:
    """
    Decrypt the media files belonging to a set of chats.

    Querying multiple JIDs in one decrypt pass is much cheaper than
    looping `decrypt_whatsapp(..., chat_jid=jid)` because
    `eb.extract_files` walks the Manifest.db exactly once. Used by the
    plan-driven Phase 5 (materialize) for favorites / multi-chat scopes.
    """
    stats = DecryptStats(chat_jid=None)
    factory = eb_factory or _open_encrypted_backup
    eb = factory(backup_dir, password)

    all_relpaths: list[str] = []
    for jid in jids:
        try:
            rps = list_chat_media_relpaths(chatstorage_path, jid)
        except ValueError:
            # Unknown JID — skip. The extract step will surface it.
            continue
        all_relpaths.extend(rps)
    # Dedup while preserving order — same media can be referenced from
    # multiple chats (forwarded images, group/individual overlap).
    seen: set[str] = set()
    unique = [rp for rp in all_relpaths if not (rp in seen or seen.add(rp))]

    stats.media_total_candidates = len(unique)
    stats.media_relpaths = unique

    if not unique:
        return stats

    decrypted, skipped_cached, missing = decrypt_media_for_relpaths(
        eb, unique, out_dir / "media", incremental=incremental,
    )
    stats.media_decrypted = decrypted
    stats.media_skipped_cached = skipped_cached
    if missing:
        stats.errors.append(
            f"{len(missing)} relpath(s) not found in Manifest.db: "
            + ", ".join(missing[:5])
            + ("…" if len(missing) > 5 else "")
        )
    return stats


def decrypt_whatsapp(
    backup_dir: Path,
    password: str,
    out_dir: Path,
    chat_jid: Optional[str] = None,
    *,
    incremental: bool = True,
    eb_factory: Optional[Callable[[Path, str], object]] = None,
) -> DecryptStats:
    """
    Top-level entry point. Returns a DecryptStats instance.

    `eb_factory` lets tests inject a fake EncryptedBackup without needing
    the real library installed.
    """
    stats = DecryptStats(chat_jid=chat_jid)
    factory = eb_factory or _open_encrypted_backup
    eb = factory(backup_dir, password)

    chatstorage = extract_chatstorage(eb, out_dir)
    stats.chatstorage_extracted = chatstorage.exists()

    if chat_jid is None:
        # 3A path — decrypt everything in the WhatsApp shared domain.
        written = eb.extract_files(
            domain_like=WHATSAPP_SHARED_DOMAIN,
            output_folder=str(out_dir / "media"),
            preserve_folders=True,
            incremental=incremental,
        )
        stats.media_decrypted = int(written or 0)
        return stats

    # 3B path — only this chat's media.
    relpaths = list_chat_media_relpaths(chatstorage, chat_jid)
    stats.media_total_candidates = len(relpaths)
    stats.media_relpaths = relpaths

    if not relpaths:
        # Chat exists but has no media — text-only DM. Nothing more to do.
        return stats

    decrypted, skipped_cached, missing = decrypt_media_for_relpaths(
        eb,
        relpaths,
        out_dir / "media",
        incremental=incremental,
    )
    stats.media_decrypted = decrypted
    stats.media_skipped_cached = skipped_cached
    if missing:
        stats.errors.append(
            f"{len(missing)} relpath(s) not found in Manifest.db: "
            + ", ".join(missing[:5])
            + ("…" if len(missing) > 5 else "")
        )
    return stats


# ─── CLI ────────────────────────────────────────────────────────────────────


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Selective WhatsApp decryption (3A: domain-only, 3B: chat-only)",
    )
    parser.add_argument("--backup-dir", required=True, type=Path,
                        help="Path to <BACKUP>/<UDID>/ (the encrypted backup tree)")
    parser.add_argument("--password-env", default="BACKUP_PASSWORD",
                        help="Env var holding the backup password (default: BACKUP_PASSWORD)")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="Destination for decrypted artifacts")
    parser.add_argument("--chat-jid",
                        help="If set, only decrypt this chat's media (3B)")
    parser.add_argument("--no-incremental", action="store_true",
                        help="Re-decrypt files even if a fresher copy exists in out-dir")
    args = parser.parse_args(argv)

    password = os.environ.get(args.password_env)
    if not password:
        print(f"[ERROR] env var {args.password_env} is empty", file=sys.stderr)
        return 1

    try:
        stats = decrypt_whatsapp(
            args.backup_dir,
            password,
            args.out_dir,
            chat_jid=args.chat_jid,
            incremental=not args.no_incremental,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    except Exception as e:  # pragma: no cover — surface library errors clearly
        msg = str(e).lower()
        if "password" in msg or "passphrase" in msg or "decrypt" in msg:
            print(f"[ERROR] Decryption failed — wrong password: {e}", file=sys.stderr)
        else:
            print(f"[ERROR] Decryption failed: {e}", file=sys.stderr)
        return 1

    if stats.chat_jid:
        print(f"[INFO] Chat-scoped decrypt for {stats.chat_jid}: "
              f"candidates={stats.media_total_candidates}, "
              f"decrypted={stats.media_decrypted}, "
              f"skipped_cached={stats.media_skipped_cached}")
    else:
        print(f"[INFO] Domain-scoped decrypt: decrypted={stats.media_decrypted}")

    for err in stats.errors:
        print(f"[WARN] {err}", file=sys.stderr)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
