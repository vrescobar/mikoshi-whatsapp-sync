"""mtime-keyed cache for the COUNT(*) probes in Source.snapshot().

These are the long pole for TUI header startup on big DBs. Cached
correctly: identical (path, mtime_ns) returns the cached counts without
hitting SQLite again; a touched DB (different mtime) re-reads.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import MacLiveSource  # noqa: E402
from sources import base as sources_base  # noqa: E402


def _make_db(path: Path, n_messages: int = 5, n_media: int = 2) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT);
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
            ZSTANZAID TEXT, ZMESSAGEDATE REAL
        );
        CREATE TABLE ZWAMEDIAITEM (
            Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZMEDIALOCALPATH TEXT
        );
        INSERT INTO ZWACHATSESSION VALUES (1, 'alice@s.whatsapp.net');
        """
    )
    for i in range(n_messages):
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (?, 1, ?, ?)",
            (i + 1, f"S{i}", float(i)),
        )
    for i in range(n_media):
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM VALUES (?, ?, 'Media/m.jpg')",
            (i + 1, i + 1),
        )
    conn.commit()
    conn.close()


def test_repeated_snapshots_hit_cache(tmp_path, monkeypatch):
    sources_base._COUNT_CACHE.clear()
    db = tmp_path / "ChatStorage.sqlite"
    _make_db(db, n_messages=7, n_media=3)
    src = MacLiveSource(root=tmp_path)

    snap1 = src.snapshot()
    assert snap1.message_count == 7
    assert snap1.media_with_local_path == 3

    # Patch sqlite3.connect to fail; cached call must succeed regardless.
    original_connect = sqlite3.connect

    def boom(*args, **kwargs):
        raise AssertionError("snapshot() reconnected despite cache hit")

    monkeypatch.setattr(sqlite3, "connect", boom)
    try:
        snap2 = src.snapshot()
    finally:
        monkeypatch.setattr(sqlite3, "connect", original_connect)
    assert snap2.message_count == 7
    assert snap2.media_with_local_path == 3


def test_touching_db_invalidates_cache(tmp_path):
    sources_base._COUNT_CACHE.clear()
    db = tmp_path / "ChatStorage.sqlite"
    _make_db(db, n_messages=4, n_media=1)
    src = MacLiveSource(root=tmp_path)

    snap1 = src.snapshot()
    assert snap1.message_count == 4

    # Sleep 10ms to guarantee a distinct mtime_ns then add rows.
    time.sleep(0.01)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO ZWAMESSAGE VALUES (99, 1, 'S99', 999.0)")
    conn.commit()
    conn.close()

    snap2 = src.snapshot()
    assert snap2.message_count == 5


def test_cache_evicts_stale_mtime_entry(tmp_path):
    """When the file mtime changes, the cache shouldn't keep ballooning
    — we drop the previous entry for the same path."""
    sources_base._COUNT_CACHE.clear()
    db = tmp_path / "ChatStorage.sqlite"
    _make_db(db, n_messages=1)
    src = MacLiveSource(root=tmp_path)
    src.snapshot()
    time.sleep(0.01)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO ZWAMESSAGE VALUES (50, 1, 'X', 0.0)")
    conn.commit()
    conn.close()
    src.snapshot()
    # Only one entry should remain for this path.
    keys_for_db = [k for k in sources_base._COUNT_CACHE if k[0] == str(db)]
    assert len(keys_for_db) == 1
