"""Tests for the socket-resilient retry, manifest batching, heartbeat, and
runtime-cap behaviour added to push_via_api.py after the May 2026 sync
failures (BrokenPipe on /manifest, TimeoutError on /commit).

Transport tests use a loopback HTTP server or monkeypatched failures:

  * transient socket errors are retried, HTTP non-2xx is not
  * retry-exhaustion produces a decoded one-line message, never a traceback
  * manifest splits keep top-level fields, never split inside a chat
  * heartbeat prints proportionally to elapsed time
  * --max-runtime-seconds aborts between batches with exit code 4
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import push_via_api  # noqa: E402




@pytest.fixture
def redirect_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path, self.headers.get("Authorization")))
            if self.path.startswith("/redirect/"):
                self.send_response(self.server.redirect_status)
                self.send_header("Location", "/target")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.redirect_status = 301
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/redirect", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_redirect_does_not_rewrite_request_or_forward_token(redirect_server, status, method):
    server, url, requests = redirect_server
    server.redirect_status = status
    actual, raw = push_via_api.http_request(
        url + "/manifest", method, "secret",
        body=b"{}" if method == "POST" else None,
    )
    assert actual == status
    assert json.loads(raw) == {"location": "/target"}
    assert requests == [(method, "/redirect/manifest", "Bearer secret")]


def test_redirect_preflight_blocks_manifest_and_preserves_cursor(redirect_server, tmp_path, monkeypatch):
    _, url, requests = redirect_server
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest([_chat("a@s", 1, msg_size=500_000)])))
    state = tmp_path / "state.json"
    state.write_text('{"unchanged": true}')
    monkeypatch.setattr(sys, "argv", [
        "push_via_api", "--manifest", str(manifest),
        "--attachments-dir", str(tmp_path), "--state-file", str(state),
        "--url", url, "--token", "secret",
    ])
    assert push_via_api.main() == 1
    assert requests == [("GET", "/redirect/api/ingest/v1/cursor", "Bearer secret")]
    assert json.loads(state.read_text()) == {"unchanged": True}


# ─── request_with_retry ──────────────────────────────────────────────────


class TestRequestWithRetry:
    def test_returns_sentinel_after_exhaustion(self, monkeypatch):
        calls = {"n": 0}

        def fake(*a, **kw):
            calls["n"] += 1
            raise BrokenPipeError("simulated")

        monkeypatch.setattr(push_via_api, "http_request", fake)
        status, body = push_via_api.request_with_retry(
            url="http://x", method="POST", token="t", body=b"{}",
            content_type="application/json", timeout=10.0,
            max_retries=2, retry_label="manifest", sleep_s=0.0,
        )
        assert status == 0
        assert calls["n"] == 3  # max_retries=2 → 3 total attempts
        assert body["_socket_error"]["kind"] == "broken_pipe"
        assert body["_socket_error"]["operation"] == "manifest"
        assert body["_socket_error"]["attempts"] == 3

    def test_success_on_second_attempt(self, monkeypatch):
        calls = {"n": 0}

        def fake(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("transient")
            return 200, b'{"ok":true}'

        monkeypatch.setattr(push_via_api, "http_request", fake)
        status, raw = push_via_api.request_with_retry(
            url="http://x", method="POST", token="t", body=b"{}",
            content_type="application/json", timeout=10.0,
            max_retries=2, retry_label="commit", sleep_s=0.0,
        )
        assert status == 200
        assert raw == b'{"ok":true}'
        assert calls["n"] == 2

    def test_http_error_status_not_retried(self, monkeypatch):
        # http_request already catches HTTPError and returns (code, body).
        # request_with_retry must NOT retry these — they're deterministic.
        calls = {"n": 0}

        def fake(*a, **kw):
            calls["n"] += 1
            return 500, b'{"error":"boom"}'

        monkeypatch.setattr(push_via_api, "http_request", fake)
        status, raw = push_via_api.request_with_retry(
            url="http://x", method="POST", token="t", body=b"{}",
            content_type="application/json", timeout=10.0,
            max_retries=3, retry_label="manifest", sleep_s=0.0,
        )
        assert status == 500
        assert calls["n"] == 1

    def test_url_error_wrapping_timeout_classifies_as_timeout(self, monkeypatch):
        def fake(*a, **kw):
            raise urllib.error.URLError(TimeoutError("read timed out"))

        monkeypatch.setattr(push_via_api, "http_request", fake)
        status, body = push_via_api.request_with_retry(
            url="http://x", method="POST", token="t", body=b"{}",
            content_type="application/json", timeout=10.0,
            max_retries=0, retry_label="commit", sleep_s=0.0,
        )
        assert status == 0
        assert body["_socket_error"]["kind"] == "timeout"


# ─── post_json (sentinel pass-through) ───────────────────────────────────


class TestPostJsonSentinel:
    def test_sentinel_passed_through_without_jsondecode(self, monkeypatch):
        def fake(*a, **kw):
            raise BrokenPipeError("simulated")

        monkeypatch.setattr(push_via_api, "http_request", fake)
        status, body = push_via_api.post_json(
            "http://x", "t", {"k": "v"}, timeout=1.0,
            retry_label="manifest", max_retries=0,
        )
        assert status == 0
        assert "_socket_error" in body
        # decode_socket_error must accept it directly.
        msg = push_via_api.decode_socket_error(body)
        assert msg.startswith("[ERROR]")


# ─── split_manifest_by_size ──────────────────────────────────────────────


def _chat(jid: str, n_msgs: int, msg_size: int = 100) -> dict:
    return {
        "jid": jid,
        "name": jid.split("@")[0],
        "messages": [
            {
                "external_id": f"ios:{jid}:{i}",
                "timestamp": f"2026-05-29T00:00:{i:02d}+00:00",
                "text": "x" * msg_size,
            }
            for i in range(n_msgs)
        ],
    }


def _manifest(chats: list[dict]) -> dict:
    return {"schema_version": "1.2", "account_id": "acc-1", "chats": chats}


class TestSplitManifestBySize:
    def test_single_batch_when_under_thresholds(self):
        m = _manifest([_chat("a@s", 2), _chat("b@s", 2)])
        batches = push_via_api.split_manifest_by_size(m, max_bytes=10_000_000, max_messages=10_000)
        assert len(batches) == 1
        assert batches[0]["chats"] == m["chats"]
        assert batches[0]["schema_version"] == "1.2"
        assert batches[0]["account_id"] == "acc-1"

    def test_splits_on_byte_threshold(self):
        chats = [_chat(f"c{i}@s", 1, msg_size=4000) for i in range(5)]
        m = _manifest(chats)
        # Each chat ~4KB → cap at 8KB should give ~3 batches.
        batches = push_via_api.split_manifest_by_size(m, max_bytes=8_000, max_messages=10_000)
        assert len(batches) >= 2
        # Top-level fields preserved on every batch.
        for b in batches:
            assert b["schema_version"] == "1.2"
            assert b["account_id"] == "acc-1"
        # Union of chats across batches equals input.
        all_jids = [c["jid"] for b in batches for c in b["chats"]]
        assert all_jids == [c["jid"] for c in chats]

    def test_splits_on_message_threshold(self):
        chats = [_chat(f"c{i}@s", 30, msg_size=10) for i in range(5)]
        m = _manifest(chats)
        batches = push_via_api.split_manifest_by_size(m, max_bytes=10_000_000, max_messages=50)
        # Each chat has 30 msgs, cap 50 → one full chat per batch (30+30=60 > 50).
        assert len(batches) == 5
        for b in batches:
            assert len(b["chats"]) == 1

    def test_disabled_when_max_bytes_zero(self):
        m = _manifest([_chat("a@s", 2)])
        batches = push_via_api.split_manifest_by_size(m, max_bytes=0, max_messages=100)
        assert len(batches) == 1
        assert batches[0] is m

    def test_disabled_when_max_messages_zero(self):
        m = _manifest([_chat("a@s", 2)])
        batches = push_via_api.split_manifest_by_size(m, max_bytes=1_000_000, max_messages=0)
        assert len(batches) == 1
        assert batches[0] is m

    def test_oversize_single_chat_emitted_alone(self):
        # A 5KB chat alone exceeds a 1KB byte cap — it must still be emitted.
        chats = [_chat("a@s", 1, msg_size=10), _chat("big@s", 1, msg_size=5_000)]
        m = _manifest(chats)
        batches = push_via_api.split_manifest_by_size(m, max_bytes=1_000, max_messages=10_000)
        assert len(batches) == 2
        # `big@s` ends up alone in its own batch.
        big_batches = [b for b in batches if any(c["jid"] == "big@s" for c in b["chats"])]
        assert len(big_batches) == 1
        assert [c["jid"] for c in big_batches[0]["chats"]] == ["big@s"]

    def test_no_chats_returns_input_unchanged(self):
        m = _manifest([])
        batches = push_via_api.split_manifest_by_size(m, max_bytes=1000, max_messages=100)
        assert batches == [m]


# ─── cursors_from_manifest_dict ──────────────────────────────────────────


class TestCursorsFromManifestDict:
    def test_picks_max_timestamp_per_chat(self):
        m = _manifest([
            {
                "jid": "a@s",
                "messages": [
                    {"external_id": "x1", "timestamp": "2026-05-29T00:00:00+00:00"},
                    {"external_id": "x3", "timestamp": "2026-05-29T00:00:02+00:00"},
                    {"external_id": "x2", "timestamp": "2026-05-29T00:00:01+00:00"},
                ],
            },
        ])
        out = push_via_api.cursors_from_manifest_dict(m)
        assert out == {"a@s": {"ts": "2026-05-29T00:00:02+00:00", "external_id": "x3"}}

    def test_skips_chat_with_no_timestamped_messages(self):
        m = _manifest([{"jid": "a@s", "messages": [{"external_id": "x", "timestamp": None}]}])
        assert push_via_api.cursors_from_manifest_dict(m) == {}


# ─── CommitHeartbeat ─────────────────────────────────────────────────────


class TestCommitHeartbeat:
    def test_emits_lines_when_block_runs_past_interval(self, capsys):
        with push_via_api.CommitHeartbeat(label="commit", interval_s=0.05):
            time.sleep(0.18)
        out = capsys.readouterr().out
        assert "still waiting for commit response" in out
        assert "elapsed" in out
        # Final-line summary fires when elapsed > interval.
        assert "commit response received after" in out

    def test_silent_when_block_finishes_fast(self, capsys):
        with push_via_api.CommitHeartbeat(label="commit", interval_s=10.0):
            pass
        out = capsys.readouterr().out
        assert "still waiting" not in out
        # And no spurious summary line either.
        assert "received after" not in out


# ─── main: --max-runtime-seconds enforced between batches ────────────────


class TestMaxRuntimeSeconds:
    def _setup_manifest_and_args(self, tmp_path, monkeypatch, runtime_seconds: int):
        # Write a real on-disk manifest with two chats so split produces 2 batches.
        chats = [_chat("a@s", 5, msg_size=1000), _chat("b@s", 5, msg_size=1000)]
        manifest = _manifest(chats)
        mf = tmp_path / "m.json"
        mf.write_text(json.dumps(manifest))
        ad = tmp_path / "attach"
        ad.mkdir()
        state = tmp_path / "state.json"
        monkeypatch.setenv("MIKOSHI_URL", "http://x")
        monkeypatch.setattr(push_via_api, "test_auth", lambda *args: (True, "OK"))
        monkeypatch.setenv("MIKOSHI_TOKEN", "t")
        argv = [
            "push_via_api",
            "--manifest", str(mf),
            "--attachments-dir", str(ad),
            "--state-file", str(state),
            "--no-cursor-write",
            "--batch-bytes", "1000",  # forces split
            "--batch-messages", "5",
            "--max-runtime-seconds", str(runtime_seconds),
            "--heartbeat-interval", "10",
        ]
        monkeypatch.setattr(sys, "argv", argv)

    def test_aborts_between_batches_when_deadline_passed(self, tmp_path, monkeypatch, capsys):
        self._setup_manifest_and_args(tmp_path, monkeypatch, runtime_seconds=1)

        calls = {"n": 0}

        def fake_push_one_batch(**kw):
            calls["n"] += 1
            # First batch takes longer than the cap; second batch is reached
            # only via the deadline check between iterations.
            time.sleep(1.2)
            return 0

        monkeypatch.setattr(push_via_api, "push_one_batch", fake_push_one_batch)
        rc = push_via_api.main()
        err = capsys.readouterr().err
        assert rc == 4
        assert "--max-runtime-seconds elapsed" in err
        # One batch ran; deadline check at iteration 2 aborted before push_one_batch was called again.
        assert calls["n"] == 1

    def test_no_cap_runs_all_batches(self, tmp_path, monkeypatch):
        self._setup_manifest_and_args(tmp_path, monkeypatch, runtime_seconds=0)

        calls = {"n": 0}

        def fake_push_one_batch(**kw):
            calls["n"] += 1
            return 0

        monkeypatch.setattr(push_via_api, "push_one_batch", fake_push_one_batch)
        rc = push_via_api.main()
        assert rc == 0
        assert calls["n"] == 2  # two batches expected from the split
