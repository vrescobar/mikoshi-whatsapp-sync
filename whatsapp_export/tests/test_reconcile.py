"""Tests for the --reconcile manifest-trim logic in push_via_api.

No network: push_via_api.post_json is monkeypatched. The contract pinned:

  * a message is kept iff the server is missing it OR it carries a media blob
    the server is missing (present message + lost server blob → re-upload)
  * chats that end empty are dropped; stats are recomputed
  * a 404 (old server without /reconcile) leaves the chat untouched
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import push_via_api  # noqa: E402


def _manifest():
    return {
        "schema_version": "1.2",
        "exported_at": "2026-06-06T00:00:00Z",
        "mode": "full",
        "chats": [
            {
                "jid": "123@g.us",
                "name": "G",
                "messages": [
                    {"external_id": "wa:A", "timestamp": "t1"},
                    {"external_id": "wa:B", "timestamp": "t2", "attachment": {"skipped": False, "sha256": "x" * 64}},
                    {"external_id": "wa:C", "timestamp": "t3", "attachment": {"skipped": False, "sha256": "y" * 64}},
                ],
            }
        ],
        "stats": {"total_chats": 1, "total_messages": 3, "attachments_kept": 2, "attachments_skipped": 0},
    }


def test_keeps_missing_messages_and_messages_with_missing_media(monkeypatch):
    # Server has A and C; is missing B (message). It also lacks blob "xxxx" (B's).
    # → keep B (missing message AND missing media). Drop A and C.
    def fake_post(endpoint, token, payload, **kw):
        assert endpoint.endswith("/api/ingest/v1/reconcile")
        return 200, {"missing_messages": ["wa:B"], "missing_media": ["x" * 64]}

    monkeypatch.setattr(push_via_api, "post_json", fake_post)
    out = push_via_api.reconcile_manifest("http://srv", "tok", _manifest())
    kept = [m["external_id"] for m in out["chats"][0]["messages"]]
    assert kept == ["wa:B"]
    assert out["stats"]["total_messages"] == 1
    assert out["stats"]["total_chats"] == 1


def test_present_message_with_lost_server_blob_is_resent(monkeypatch):
    # Server has every message, but lost C's blob "yyyy". No missing messages.
    # → keep C only (its media must be re-uploaded), drop A and B.
    def fake_post(endpoint, token, payload, **kw):
        return 200, {"missing_messages": [], "missing_media": ["y" * 64]}

    monkeypatch.setattr(push_via_api, "post_json", fake_post)
    out = push_via_api.reconcile_manifest("http://srv", "tok", _manifest())
    kept = [m["external_id"] for m in out["chats"][0]["messages"]]
    assert kept == ["wa:C"]


def test_nothing_missing_drops_the_chat(monkeypatch):
    monkeypatch.setattr(
        push_via_api, "post_json", lambda *a, **k: (200, {"missing_messages": [], "missing_media": []})
    )
    out = push_via_api.reconcile_manifest("http://srv", "tok", _manifest())
    assert out["chats"] == []
    assert out["stats"]["total_messages"] == 0


def test_404_leaves_chat_untouched(monkeypatch):
    monkeypatch.setattr(push_via_api, "post_json", lambda *a, **k: (404, {"error": "not_found"}))
    out = push_via_api.reconcile_manifest("http://srv", "tok", _manifest())
    assert len(out["chats"][0]["messages"]) == 3  # full push fallback
