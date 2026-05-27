"""Tests for the redesigned cursor model in pipeline_state.py.

The two scenarios that *must* never regress:

1. A push failure must not advance any cursor.
2. Loading a v1 (legacy) cache must yield a working v2-shaped object
   so the rest of the pipeline can keep operating during migration.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pipeline_state


# ─── cache I/O ────────────────────────────────────────────────────────────


class TestCursorCacheIO:
    def test_load_missing_returns_empty(self, tmp_path):
        cache = pipeline_state.load_cursor_cache(tmp_path / "nope.json")
        assert cache.chats == {}
        assert cache.version == 2

    def test_load_v1_legacy_format(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text(json.dumps({
            "version": 1,
            "last_global_sync": "2026-05-25T10:00:00+00:00",
            "chats": {
                "alice@s.whatsapp.net": "2026-05-24T18:00:00+00:00",
                "bob@s.whatsapp.net": "2026-05-23T12:00:00+00:00",
            },
        }))
        cache = pipeline_state.load_cursor_cache(f)
        # v1 entries are flagged as legacy so drift detection knows to re-verify
        assert "alice@s.whatsapp.net" in cache.chats
        assert cache.chats["alice@s.whatsapp.net"].source == pipeline_state.SOURCE_EXTRACTED_LEGACY
        assert cache.chats["alice@s.whatsapp.net"].committed_through_ts == "2026-05-24T18:00:00+00:00"

    def test_load_v2_native_format(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text(json.dumps({
            "version": 2,
            "server_url": "https://x",
            "last_successful_commit": "2026-05-26T18:42:00Z",
            "chats": {
                "alice@s.whatsapp.net": {
                    "committed_through_ts": "2026-05-26T07:14:17+00:00",
                    "committed_through_external_id": "ios:1834219",
                    "source": "server",
                },
            },
        }))
        cache = pipeline_state.load_cursor_cache(f)
        a = cache.chats["alice@s.whatsapp.net"]
        assert a.source == "server"
        assert a.committed_through_external_id == "ios:1834219"

    def test_corrupt_file_is_quarantined(self, tmp_path):
        """A corrupt cache file must not crash the pipeline.

        Regression guard: the load path used to raise json.JSONDecodeError
        which propagated all the way up. Worse, the file stuck around and
        broke every subsequent run too. Now we quarantine it and return
        an empty cache so the next successful push can land cleanly.
        """
        f = tmp_path / "state.json"
        f.write_text("{not valid json")
        cache = pipeline_state.load_cursor_cache(f)
        assert cache.chats == {}
        # Original file renamed out of the way
        assert not f.exists() or f.read_text() != "{not valid json"
        siblings = list(tmp_path.glob("state.json.broken-*"))
        assert siblings, "Expected the corrupt file to be quarantined"

    def test_save_and_reload_roundtrip(self, tmp_path):
        f = tmp_path / "state.json"
        cache = pipeline_state.CursorCache(
            server_url="https://x",
            last_successful_commit="2026-05-26T18:42:00+00:00",
            last_push_id="01HXY",
        )
        cache.chats["alice@s.whatsapp.net"] = pipeline_state.ChatCursor(
            committed_through_ts="2026-05-26T07:14:17+00:00",
            committed_through_external_id="ios:42",
            source="server",
        )
        pipeline_state.save_cursor_cache(f, cache)
        reloaded = pipeline_state.load_cursor_cache(f)
        assert reloaded.last_push_id == "01HXY"
        assert reloaded.chats["alice@s.whatsapp.net"].committed_through_external_id == "ios:42"
        # Always v2 on disk
        on_disk = json.loads(f.read_text())
        assert on_disk["version"] == 2


# ─── the core invariant: failed push must not advance cursors ────────────


class TestCursorOnlyAdvancesOnCommit:
    def test_update_from_commit_writes_server_cursors(self, tmp_path):
        f = tmp_path / "state.json"
        cache = pipeline_state.update_cache_from_commit(
            state_file=f,
            server_url="https://x",
            push_id="01HXY",
            committed_cursors={
                "alice@s.whatsapp.net": {
                    "ts": "2026-05-26T07:14:17+00:00",
                    "external_id": "ios:1834219",
                },
            },
        )
        assert cache.chats["alice@s.whatsapp.net"].source == "server"
        assert cache.chats["alice@s.whatsapp.net"].committed_through_external_id == "ios:1834219"

    def test_extraction_time_save_is_noop_by_default(self, tmp_path, monkeypatch, capsys):
        """The historical drift bug: extract_messages.save_sync_state used to
        write cursors before push had been attempted. The redesign makes
        that call a no-op unless MIKOSHI_TRUST_LOCAL_CURSOR=1.
        """
        import extract_messages

        # Make sure the escape hatch is NOT set
        monkeypatch.delenv("MIKOSHI_TRUST_LOCAL_CURSOR", raising=False)
        f = tmp_path / "state.json"

        # Pre-existing v2 cache the (would-be) extraction must not modify.
        pre = pipeline_state.CursorCache(server_url="https://x")
        pre.chats["alice@s.whatsapp.net"] = pipeline_state.ChatCursor(
            committed_through_ts="2026-05-20T00:00:00+00:00", source="server"
        )
        pipeline_state.save_cursor_cache(f, pre)

        # Simulate extraction producing a *would-be* advance.
        extract_messages.save_sync_state(f, {
            "last_global_sync": "2026-05-26T18:42:00+00:00",
            "chats": {"alice@s.whatsapp.net": "2026-05-26T07:00:00+00:00"},
        })
        out = capsys.readouterr()
        assert "Skipping cursor write" in out.err

        # Cache on disk must be unchanged from the seeded value.
        reloaded = pipeline_state.load_cursor_cache(f)
        assert reloaded.chats["alice@s.whatsapp.net"].committed_through_ts == "2026-05-20T00:00:00+00:00"
        assert reloaded.chats["alice@s.whatsapp.net"].source == "server"

    def test_extraction_time_save_writes_when_escape_hatch_is_on(self, tmp_path, monkeypatch, capsys):
        """The legacy behaviour must still be reachable for users who want it,
        but it's gated behind an explicit env var that prints a loud warning.
        """
        import extract_messages

        monkeypatch.setenv("MIKOSHI_TRUST_LOCAL_CURSOR", "1")
        f = tmp_path / "state.json"

        extract_messages.save_sync_state(f, {
            "last_global_sync": "2026-05-26T18:42:00+00:00",
            "chats": {"alice@s.whatsapp.net": "2026-05-26T07:00:00+00:00"},
        })
        out = capsys.readouterr()
        assert "TRUST_LOCAL_CURSOR" in out.err

        reloaded = pipeline_state.load_cursor_cache(f)
        # Tagged so drift detection knows to re-verify
        assert reloaded.chats["alice@s.whatsapp.net"].source == pipeline_state.SOURCE_EXTRACTED_LEGACY

    def test_save_never_rewinds_an_existing_cursor(self, tmp_path, monkeypatch):
        """With the escape hatch on, extraction-time writes still must NOT
        rewind a cursor that the server (via a successful push) already
        moved further forward."""
        import extract_messages

        monkeypatch.setenv("MIKOSHI_TRUST_LOCAL_CURSOR", "1")
        f = tmp_path / "state.json"

        pre = pipeline_state.CursorCache(server_url="https://x")
        pre.chats["alice@s.whatsapp.net"] = pipeline_state.ChatCursor(
            committed_through_ts="2026-05-26T18:42:00+00:00", source="server"
        )
        pipeline_state.save_cursor_cache(f, pre)

        # Older value from a stale extraction attempts to overwrite — should be ignored.
        extract_messages.save_sync_state(f, {
            "last_global_sync": "2026-05-26T19:00:00+00:00",
            "chats": {"alice@s.whatsapp.net": "2026-05-20T00:00:00+00:00"},
        })

        reloaded = pipeline_state.load_cursor_cache(f)
        assert reloaded.chats["alice@s.whatsapp.net"].committed_through_ts == "2026-05-26T18:42:00+00:00"
        assert reloaded.chats["alice@s.whatsapp.net"].source == "server"  # untouched


# ─── drift detection ──────────────────────────────────────────────────────


class TestDriftDetection:
    def _cache(self, **chats):
        c = pipeline_state.CursorCache()
        for jid, ts in chats.items():
            c.chats[jid] = pipeline_state.ChatCursor(committed_through_ts=ts, source="server")
        return c

    def _srv(self, **chats):
        return {
            jid: pipeline_state.ChatCursor(committed_through_ts=ts, source="server")
            for jid, ts in chats.items()
        }

    def test_in_sync(self):
        cache = self._cache(alice="2026-05-26T07:14:17+00:00")
        srv = self._srv(alice="2026-05-26T07:14:17+00:00")
        report = pipeline_state.detect_drift(cache, srv)
        assert len(report) == 1
        assert report[0].status == pipeline_state.DriftStatus.IN_SYNC

    def test_local_ahead_is_flagged(self):
        """The original bug: local advanced past a failed push.

        Without this signal the user has no idea drift exists.
        """
        cache = self._cache(alice="2026-05-26T18:42:00+00:00")
        srv = self._srv(alice="2026-05-25T09:13:00+00:00")
        report = pipeline_state.detect_drift(cache, srv)
        assert report[0].status == pipeline_state.DriftStatus.LOCAL_AHEAD

    def test_server_ahead_when_another_client_pushed(self):
        cache = self._cache(alice="2026-05-20T00:00:00+00:00")
        srv = self._srv(alice="2026-05-26T07:14:17+00:00")
        report = pipeline_state.detect_drift(cache, srv)
        assert report[0].status == pipeline_state.DriftStatus.SERVER_AHEAD

    def test_no_server_record(self):
        cache = self._cache(alice="2026-05-26T07:14:17+00:00")
        srv = self._srv()  # empty
        report = pipeline_state.detect_drift(cache, srv)
        assert report[0].status == pipeline_state.DriftStatus.NO_SERVER_RECORD

    def test_server_endpoint_unreachable(self):
        """server=None (not empty dict!) means we couldn't reach the server.

        Drift must still be reported per chat, with a clear status label
        so the UI can show 'unknown' rather than claiming in-sync.
        """
        cache = self._cache(alice="2026-05-26T07:14:17+00:00")
        report = pipeline_state.detect_drift(cache, None)
        assert len(report) == 1
        assert report[0].status == pipeline_state.DriftStatus.NO_SERVER_RECORD
        assert "unreachable" in report[0].note


# ─── _best_from_phase (moved from tui.py — verify the wrapper still works) ─


class TestBestFromPhase:
    def test_no_backup_dir_returns_phase_1(self, tmp_path):
        phase, label = pipeline_state.best_from_phase(None)
        assert phase == 1
        assert "iPhone" in label

    def test_encrypted_backup_returns_phase_3(self, tmp_path):
        udid = tmp_path / "backup" / "00008130-00011234567890ABCDEF"
        udid.mkdir(parents=True)
        (udid / "Manifest.plist").write_bytes(b"bplist00" + b"\x00" * 1000)
        phase, label = pipeline_state.best_from_phase(tmp_path)
        assert phase == 3
        assert "no iPhone" in label.lower() or "re-decrypt" in label.lower()

    def test_decrypted_db_returns_phase_4(self, tmp_path):
        udid = tmp_path / "backup" / "00008130-00011234567890ABCDEF"
        udid.mkdir(parents=True)
        (udid / "Manifest.plist").write_bytes(b"bplist00" + b"\x00" * 1000)
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "ChatStorage.sqlite").write_bytes(b"SQLite format 3\x00")
        phase, label = pipeline_state.best_from_phase(tmp_path)
        assert phase == 4
        assert "extract-only" in label.lower()

    def test_corrupt_sqlite_falls_back_to_phase_3(self, tmp_path):
        """A killed Phase-3 leaves a size-extended zero-header file.
        Must not be trusted; fall back to Phase 3."""
        udid = tmp_path / "backup" / "00008130-00011234567890ABCDEF"
        udid.mkdir(parents=True)
        (udid / "Manifest.plist").write_bytes(b"bplist00" + b"\x00" * 1000)
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        (extracted / "ChatStorage.sqlite").write_bytes(b"\x00" * 4096)
        phase, _ = pipeline_state.best_from_phase(tmp_path)
        assert phase == 3


# ─── plan computation ────────────────────────────────────────────────────


class TestComputePlan:
    """Uses the synthetic_db fixture from conftest.py."""

    def test_plan_against_empty_cache_counts_everything(self, synthetic_db):
        cache = pipeline_state.CursorCache()
        plan = pipeline_state.compute_plan(synthetic_db, cache, server={})
        per_jid = {c.jid: c for c in plan.chats}
        # Alice has 3 messages, Bob has 2, Family Group has 3 (incl. 1 system)
        assert per_jid["alice@s.whatsapp.net"].new_messages == 3
        assert per_jid["bob@s.whatsapp.net"].new_messages == 2
        # plan counts EVERY message past cutoff — system filtering happens at extract time
        assert per_jid["12345@g.us"].new_messages == 3

    def test_plan_respects_cache_cutoff(self, synthetic_db):
        cache = pipeline_state.CursorCache()
        # Alice's last message is 2026-05-24. Cutoff after that = 0 new.
        cache.chats["alice@s.whatsapp.net"] = pipeline_state.ChatCursor(
            committed_through_ts="2026-05-24T13:00:00+00:00", source="server",
        )
        plan = pipeline_state.compute_plan(synthetic_db, cache, server=None)
        per_jid = {c.jid: c for c in plan.chats}
        assert per_jid["alice@s.whatsapp.net"].new_messages == 0
        assert per_jid["bob@s.whatsapp.net"].new_messages == 2  # untouched

    def test_plan_server_cursor_wins_over_local_cache(self, synthetic_db):
        """If server says 'I have everything through TS_X' and local cache
        says TS_Y < TS_X, the plan must trust the server (TS_X)."""
        cache = pipeline_state.CursorCache()
        cache.chats["alice@s.whatsapp.net"] = pipeline_state.ChatCursor(
            committed_through_ts="2026-05-15T00:00:00+00:00",  # stale
            source=pipeline_state.SOURCE_EXTRACTED_LEGACY,
        )
        server = {
            "alice@s.whatsapp.net": pipeline_state.ChatCursor(
                committed_through_ts="2026-05-24T13:00:00+00:00", source="server",
            ),
        }
        plan = pipeline_state.compute_plan(synthetic_db, cache, server=server)
        per_jid = {c.jid: c for c in plan.chats}
        assert per_jid["alice@s.whatsapp.net"].new_messages == 0

    def test_scope_jids_filters(self, synthetic_db):
        cache = pipeline_state.CursorCache()
        plan = pipeline_state.compute_plan(
            synthetic_db, cache, server=None,
            scope_jids={"alice@s.whatsapp.net"},
        )
        assert len(plan.chats) == 1
        assert plan.chats[0].jid == "alice@s.whatsapp.net"

    def test_total_message_aggregate(self, synthetic_db):
        cache = pipeline_state.CursorCache()
        plan = pipeline_state.compute_plan(synthetic_db, cache, server={})
        assert plan.total_messages == 3 + 2 + 3
        assert plan.total_attachments == 0 + 1 + 1


# ─── server cursor fetch graceful degradation ────────────────────────────


class TestServerCursorFetch:
    def test_returns_none_on_404(self, monkeypatch):
        """Very old Mikoshi without either the /cursor or /cursors endpoint
        must not crash the client. The first 404 triggers the legacy-path
        fallback, and that also 404'ing produces None."""
        import urllib.error
        def fake_open(*a, **kw):
            raise urllib.error.HTTPError("u", 404, "not found", {}, None)
        monkeypatch.setattr("urllib.request.urlopen", fake_open)
        result = pipeline_state.fetch_server_cursors("http://x", "tok")
        assert result is None

    def test_returns_none_on_timeout(self, monkeypatch):
        """Network down → None, not exception."""
        def fake_open(*a, **kw):
            raise TimeoutError("nope")
        monkeypatch.setattr("urllib.request.urlopen", fake_open)
        assert pipeline_state.fetch_server_cursors("http://x", "tok") is None

    def test_returns_none_on_missing_url_or_token(self):
        assert pipeline_state.fetch_server_cursors("", "tok") is None
        assert pipeline_state.fetch_server_cursors("http://x", "") is None

    def test_parses_current_mikoshi_array_shape(self, monkeypatch):
        """The deployed Mikoshi server returns
        `{account_id, cursors: [{chat_jid, last_external_id, last_message_at, ...}]}`.
        The client must parse this — the M2 doc described a different
        shape, and the server team picked this one."""
        import io
        body = json.dumps({
            "account_id": "u_01",
            "cursors": [
                {
                    "chat_jid": "alice@s.whatsapp.net",
                    "last_external_id": "wa:STANZA-7",
                    "last_message_at": "2026-05-26T07:14:17Z",
                    "message_count": 3,
                    "updated_at": "2026-05-26T07:14:18Z",
                },
                {
                    "chat_jid": "bob@s.whatsapp.net",
                    "last_external_id": "ios:42",
                    "last_message_at": "2026-05-25T18:00:00Z",
                    "message_count": 1,
                    "updated_at": "2026-05-25T18:00:01Z",
                },
            ],
        }).encode()

        class FakeResp:
            status = 200
            def __init__(self): self._body = body
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *a): pass

        urls: list[str] = []
        def fake_open(req, *a, **kw):
            urls.append(req.full_url)
            return FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", fake_open)
        result = pipeline_state.fetch_server_cursors("http://x", "tok")
        assert result is not None
        # Hit /cursor (singular), not /cursors.
        assert urls[0].endswith("/api/ingest/v1/cursor")
        assert set(result.keys()) == {"alice@s.whatsapp.net", "bob@s.whatsapp.net"}
        alice = result["alice@s.whatsapp.net"]
        assert alice.committed_through_ts == "2026-05-26T07:14:17Z"
        assert alice.committed_through_external_id == "wa:STANZA-7"

    def test_falls_back_to_plural_on_first_404(self, monkeypatch):
        """If /cursor 404s (server downgraded or different version), we
        try /cursors with the legacy map-keyed shape. Keeps the client
        compatible with the M2 doc's described API while the deployed
        server uses /cursor."""
        import urllib.error
        legacy_body = json.dumps({
            "alice@s.whatsapp.net": {
                "ts": "2026-05-26T07:14:17Z",
                "external_id": "wa:STANZA-7",
            },
        }).encode()

        class FakeResp:
            status = 200
            def __init__(self, b): self._body = b
            def read(self): return self._body
            def __enter__(self): return self
            def __exit__(self, *a): pass

        call_count = {"n": 0}
        def fake_open(req, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
            return FakeResp(legacy_body)

        monkeypatch.setattr("urllib.request.urlopen", fake_open)
        result = pipeline_state.fetch_server_cursors("http://x", "tok")
        assert result is not None
        assert "alice@s.whatsapp.net" in result
        assert call_count["n"] == 2

    def test_parses_legacy_map_shape(self, monkeypatch):
        """Direct test of the map-keyed parser used by tests/older docs."""
        result = pipeline_state._parse_cursors_payload({
            "chats": {
                "alice@s.whatsapp.net": {
                    "ts": "2026-05-26T07:14:17Z",
                    "external_id": "ios:42",
                },
            },
        })
        assert "alice@s.whatsapp.net" in result
        assert result["alice@s.whatsapp.net"].committed_through_external_id == "ios:42"


# ─── server-cursor fail-fast policy ──────────────────────────────────────


class TestRequireServerCursor:
    """The redesign promotes "server is source of truth" to "server is
    mandatory". A None from fetch_server_cursors is a hard error, not a
    silent fallback to the cache — that fallback was the original
    drift-bug surface."""

    def test_returns_cursors_when_present(self):
        cursors: dict[str, pipeline_state.ChatCursor] = {}
        assert pipeline_state.require_server_cursor(cursors, "http://x") is cursors

    def test_raises_when_none_and_no_escape_hatch(self, monkeypatch):
        monkeypatch.delenv("MIKOSHI_TRUST_LOCAL_CURSOR", raising=False)
        with pytest.raises(pipeline_state.ServerCursorUnreachable) as exc:
            pipeline_state.require_server_cursor(None, "http://x")
        # Message should mention the env var, so users know how to recover.
        assert "MIKOSHI_TRUST_LOCAL_CURSOR" in str(exc.value)

    def test_degrades_when_escape_hatch_set(self, monkeypatch):
        monkeypatch.setenv("MIKOSHI_TRUST_LOCAL_CURSOR", "1")
        result = pipeline_state.require_server_cursor(None, "http://x")
        assert result == {}


# ─── 401-decoder ─────────────────────────────────────────────────────────


class TestDecodeAuthError:
    """The pre-redesign UX dumped raw JSON to stderr on every auth error
    and forced the user to debug blind. The decoder maps known shapes
    to actionable messages — see REDESIGN.md pain point #8.
    """

    def setup_method(self):
        sys.modules.pop("push_via_api", None)
        import push_via_api
        self.api = push_via_api

    def test_401_with_token_expired_phrasing(self):
        msg = self.api.decode_auth_error(401, {"error": "token has expired"})
        assert "token" in msg.lower()
        assert "regenerate" in msg.lower() or "/accounts" in msg.lower()

    def test_401_account_disabled(self):
        msg = self.api.decode_auth_error(401, {"detail": "account disabled"})
        assert "account" in msg.lower()
        assert "re-enable" in msg.lower() or "/accounts" in msg.lower()

    def test_404_endpoint_not_found(self):
        msg = self.api.decode_auth_error(404, {})
        assert "endpoint" in msg.lower() or "url" in msg.lower()

    def test_413_payload_too_large(self):
        msg = self.api.decode_auth_error(413, {})
        assert "split" in msg.lower() or "scope" in msg.lower()
