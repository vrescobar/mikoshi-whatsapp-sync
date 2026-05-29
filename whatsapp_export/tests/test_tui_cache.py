"""Tests for the on-disk TUI header cache.

The cache is a JSON projection of the live snapshot so a cold TUI launch
can repaint the header in milliseconds. Two things must hold:

  1. The projection is JSON-safe (no Path / datetime / dataclass leaks).
  2. Corrupt or version-mismatched files don't crash the TUI — they
     just fall back to "no cache".
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tui_cache  # noqa: E402


def _live_snapshot(tmp_path: Path) -> dict:
    """Build a snapshot shaped like ``_gather_header_snapshot``'s output —
    rich Python types included — so we can verify the JSON projection."""
    from types import SimpleNamespace
    return {
        "cfg": {"MIKOSHI_URL": "http://example/"},
        "iphone_reachable": True,
        "backup_dir": tmp_path / "backup",
        "backup_udid_count": 1,
        "backup_size": "12.3 GB",
        "chatstorage": tmp_path / "ChatStorage.sqlite",
        "chatstorage_mtime": datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
        "server_url": "http://example/",
        "server_cursors": {"jid1": object(), "jid2": object()},
        "server_total_msgs": 1_000_000,
        "local_max_msgs": 1_010_000,
        "last_successful_commit": "2026-05-26T23:31:17+00:00",
        "drift_summary": {"in_sync": 4, "local_ahead": 0},
        "sources": [
            {
                "name": "iphone_backup",
                "available": True,
                "snapshot": SimpleNamespace(
                    name="iphone_backup",
                    db_path=tmp_path / "ChatStorage.sqlite",
                    mtime_iso="2026-05-28T12:00:00+00:00",
                    message_count=1_010_000,
                    media_with_local_path=2000,
                ),
                "error": None,
            },
            {"name": "mac_live", "available": False, "snapshot": None, "error": None},
        ],
        "schedule_info": {
            "enabled": True,
            "hour": 6,
            "minute": 30,
            "next_fire_iso": "2026-05-29T06:30",
        },
        "last_run_summary": "cron_20260527_0630.log: sync finished (exit 0)",
    }


def test_roundtrip(tmp_path):
    snap = _live_snapshot(tmp_path)
    tui_cache.save(tmp_path, snap)
    cached = tui_cache.load(tmp_path)
    assert cached is not None
    assert cached["iphone_reachable"] is True
    assert cached["server_cursors_count"] == 2  # dict → count
    assert cached["server_total_msgs"] == 1_000_000
    assert cached["local_max_msgs"] == 1_010_000
    assert cached["last_successful_commit"] == "2026-05-26T23:31:17+00:00"
    assert cached["chatstorage_mtime_iso"] == "2026-05-28T12:00:00+00:00"
    assert cached["sources"][0]["snapshot"]["message_count"] == 1_010_000
    assert cached["sources"][1]["snapshot"] is None
    assert cached["schedule_info"]["hour"] == 6


def test_load_returns_none_when_missing(tmp_path):
    assert tui_cache.load(tmp_path) is None


def test_load_returns_none_when_corrupt(tmp_path):
    tui_cache.cache_path(tmp_path).write_text("{not json")
    assert tui_cache.load(tmp_path) is None


def test_load_returns_none_on_version_mismatch(tmp_path):
    tui_cache.cache_path(tmp_path).write_text(
        json.dumps({"_version": 999, "_ts": time.time()})
    )
    assert tui_cache.load(tmp_path) is None


def test_invalidate_removes_file(tmp_path):
    snap = _live_snapshot(tmp_path)
    tui_cache.save(tmp_path, snap)
    assert tui_cache.cache_path(tmp_path).exists()
    tui_cache.invalidate(tmp_path)
    assert not tui_cache.cache_path(tmp_path).exists()


def test_invalidate_silent_when_absent(tmp_path):
    # Must not raise even when there's nothing to remove.
    tui_cache.invalidate(tmp_path)


def test_age_helpers(tmp_path):
    snap = _live_snapshot(tmp_path)
    tui_cache.save(tmp_path, snap)
    cached = tui_cache.load(tmp_path)
    assert tui_cache.age_seconds(cached) < 5  # just written
    assert tui_cache.is_fresh(cached)
    assert tui_cache.is_usable(cached)
    assert tui_cache.age_seconds(None) == float("inf")


def test_save_is_atomic(tmp_path):
    """Atomic write means a half-written file can't sit at the canonical
    path. Verify the temp file is gone after a successful save."""
    snap = _live_snapshot(tmp_path)
    tui_cache.save(tmp_path, snap)
    leftover = tui_cache.cache_path(tmp_path).with_suffix(".json.tmp")
    assert not leftover.exists()
