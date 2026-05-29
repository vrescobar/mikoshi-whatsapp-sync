"""Decision matrix for _resolve_sources_with_fallback.

Until this lived in the TUI, picking "Both — iPhone + Mac" with no
iPhone attached hard-failed at run_pipeline.sh Phase 1. The helper now
mirrors the cron-path fallback in mikoshi-whatsapp.sh:252-260 so the
TUI degrades to whichever source is actually available.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tui  # noqa: E402


def _src_entries(*, iphone_avail: bool, mac_avail: bool) -> list[dict]:
    def snap(name):
        return SimpleNamespace(
            name=name,
            db_path=Path(f"/tmp/{name}"),
            mtime_iso="2026-05-28T00:00:00+00:00",
            message_count=1000,
            media_with_local_path=10,
        )
    return [
        {
            "name": "iphone_backup",
            "available": iphone_avail,
            "snapshot": snap("iphone_backup") if iphone_avail else None,
            "error": None,
        },
        {
            "name": "mac_live",
            "available": mac_avail,
            "snapshot": snap("mac_live") if mac_avail else None,
            "error": None,
        },
    ]


def _snap(*, iphone_reachable: bool, has_decrypted_db: bool) -> dict:
    return {
        "iphone_reachable": iphone_reachable,
        "chatstorage": Path("/tmp/chatstorage.sqlite") if has_decrypted_db else None,
    }


def test_both_iphone_reachable_no_change():
    sel = ["iphone_backup", "mac_live"]
    snap = _snap(iphone_reachable=True, has_decrypted_db=False)
    entries = _src_entries(iphone_avail=True, mac_avail=True)
    out, phase, reason = tui._resolve_sources_with_fallback(sel, snap, 1, entries)
    assert out == sel
    assert phase == 1
    assert reason is None


def test_both_iphone_unreachable_falls_back_to_mac():
    sel = ["iphone_backup", "mac_live"]
    snap = _snap(iphone_reachable=False, has_decrypted_db=False)
    entries = _src_entries(iphone_avail=False, mac_avail=True)
    out, phase, reason = tui._resolve_sources_with_fallback(sel, snap, 1, entries)
    assert out == ["mac_live"]
    assert phase == 4
    assert reason is not None
    assert "Mac-only" in reason


def test_both_iphone_unreachable_but_cached_db_at_phase4_keeps_both():
    """When the user picked an iPhone-side phase ≥ 4, the iPhone isn't
    actually needed at runtime — extraction reads the cached decrypted
    DB. No fallback necessary."""
    sel = ["iphone_backup", "mac_live"]
    snap = _snap(iphone_reachable=False, has_decrypted_db=True)
    entries = _src_entries(iphone_avail=True, mac_avail=True)
    out, phase, reason = tui._resolve_sources_with_fallback(sel, snap, 4, entries)
    assert out == sel
    assert phase == 4
    assert reason is None


def test_iphone_only_unreachable_no_cached_db_is_fatal():
    sel = ["iphone_backup"]
    snap = _snap(iphone_reachable=False, has_decrypted_db=False)
    entries = _src_entries(iphone_avail=False, mac_avail=False)
    out, phase, reason = tui._resolve_sources_with_fallback(sel, snap, 1, entries)
    assert out is None
    assert reason == "fatal"


def test_iphone_only_unreachable_with_cached_db_phase4_ok():
    sel = ["iphone_backup"]
    snap = _snap(iphone_reachable=False, has_decrypted_db=True)
    entries = _src_entries(iphone_avail=True, mac_avail=False)
    out, phase, reason = tui._resolve_sources_with_fallback(sel, snap, 4, entries)
    assert out == sel
    assert phase == 4
    assert reason is None


def test_mac_only_does_not_touch_iphone_state():
    sel = ["mac_live"]
    snap = _snap(iphone_reachable=False, has_decrypted_db=False)
    entries = _src_entries(iphone_avail=False, mac_avail=True)
    out, phase, reason = tui._resolve_sources_with_fallback(sel, snap, 4, entries)
    assert out == sel
    assert phase == 4
    assert reason is None


def test_both_iphone_unreachable_mac_unavailable_is_fatal():
    sel = ["iphone_backup", "mac_live"]
    snap = _snap(iphone_reachable=False, has_decrypted_db=False)
    entries = _src_entries(iphone_avail=False, mac_avail=False)
    out, phase, reason = tui._resolve_sources_with_fallback(sel, snap, 1, entries)
    assert out is None
    assert reason == "fatal"
