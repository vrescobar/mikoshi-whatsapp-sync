"""Test-only helpers shared between test modules.

Lives next to (not inside) conftest.py because pytest's conftest is
special-cased: regular `from conftest import X` doesn't work, but a
sibling module like `_helpers.py` imports just like any other.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def persist_cursors_like_push(state_file, chats_state):
    """
    Mimic what push_via_api.py does on a successful commit: stamp the
    cursor cache with the server-confirmed values.

    Pre-redesign, extract_messages.save_sync_state wrote the file
    directly at extraction time. That's the bug the redesign fixed —
    cursors now only advance after the server confirms a commit. Tests
    that exercise multi-run cursor semantics simulate the post-push
    step with this helper.
    """
    import pipeline_state
    committed = {
        jid: {"ts": ts, "external_id": None}
        for jid, ts in (chats_state or {}).items()
        if ts
    }
    pipeline_state.update_cache_from_commit(
        state_file=state_file,
        server_url="http://test",
        push_id="test-push",
        committed_cursors=committed,
    )
