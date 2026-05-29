#!/usr/bin/env python3
"""
Push a WhatsApp export to a Mikoshi server via the REST ingestion API.

Three-step protocol (content-addressed, idempotent):
  1. POST /api/ingest/v1/manifest  → server returns push_id + needs_media[]
  2. POST /api/ingest/v1/media/<sha256> (raw bytes) for each missing hash
  3. POST /api/ingest/v1/commit { push_id } → server persists messages +
     attachments, queues them for scan-based memory extraction, and (M2+)
     echoes back per-chat `committed_cursors` so the client can mirror
     the server's view of "what's synced."

Why cursor advancement lives here (and only here):

  Older versions of this pipeline let extract_messages.py advance
  `.sync_state.json` the moment it finished writing the manifest —
  before any push attempt. A push that 401'd then left the client
  believing data was synced while the server had nothing, and the
  next incremental run reported "0 messages, 0 attachments" with no
  warning. The fix is that *commit success* — and only commit success —
  is allowed to move the cache forward.

  See REDESIGN.md §4 for the full reasoning.

Configuration via env or ~/.mikoshi-ingest.conf:
  MIKOSHI_URL    e.g. https://mikoshi.example.com  (NO trailing slash)
  MIKOSHI_TOKEN  bearer token (generated from /accounts/<id>/ingestion in Mikoshi)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pipeline_state


# Retryable transport-level exceptions raised by urllib while writing the
# request body or reading the response. HTTPError (4xx/5xx) is NOT retryable
# here — http_request() catches it and returns (code, body) for the caller
# to dispatch deterministically via decode_auth_error().
_SOCKET_ERROR_TYPES: tuple[type[BaseException], ...] = (
    TimeoutError,
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
    urllib.error.URLError,
    OSError,
)


def _classify_socket_error(exc: BaseException) -> str:
    """Bucket a transport exception into a stable kind string.

    URLError commonly wraps the underlying socket error in .reason — unwrap
    so a timeout that arrived as URLError(socket.timeout) still classifies
    as "timeout" rather than the generic "url_error".
    """
    inner: BaseException = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        inner = exc.reason
    if isinstance(inner, TimeoutError):
        return "timeout"
    if isinstance(inner, BrokenPipeError):
        return "broken_pipe"
    if isinstance(inner, (ConnectionResetError, ConnectionAbortedError)):
        return "connection_reset"
    if isinstance(exc, urllib.error.URLError):
        return "url_error"
    return "os_error"


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    cfg_path = Path(os.environ.get("MIKOSHI_INGEST_CONF", str(Path.home() / ".mikoshi-ingest.conf")))
    if cfg_path.exists():
        for line in cfg_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("MIKOSHI_URL", "MIKOSHI_TOKEN"):
        if key in os.environ and os.environ[key]:
            cfg[key] = os.environ[key]
    return cfg


def http_request(
    url: str,
    method: str,
    token: str,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url=url, method=method, data=body)
    req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def request_with_retry(
    url: str,
    method: str,
    token: str,
    body: bytes | None,
    content_type: str | None,
    timeout: float,
    max_retries: int,
    retry_label: str,
    sleep_s: float = 5.0,
) -> tuple[int, bytes | dict]:
    """Wrap http_request with linear-backoff retry on transient socket errors.

    On success (any HTTP response — including 4xx/5xx, which the caller decodes
    via decode_auth_error), returns http_request's (status, bytes) tuple.

    After exhausting all attempts on transport-level errors, returns a sentinel
    `(0, {"_socket_error": {...}})` so the caller can route to
    decode_socket_error() instead of dealing with a raw exception. max_retries
    is the number of *retries* — total attempts = max_retries + 1.
    """
    attempts = max_retries + 1
    last_kind = "unknown"
    last_detail = ""
    for attempt in range(1, attempts + 1):
        try:
            return http_request(
                url=url,
                method=method,
                token=token,
                body=body,
                content_type=content_type,
                timeout=timeout,
            )
        except _SOCKET_ERROR_TYPES as exc:
            last_kind = _classify_socket_error(exc)
            last_detail = str(exc) or type(exc).__name__
            if attempt >= attempts:
                break
            print(
                f"[WARN] {retry_label}: {last_kind} on attempt {attempt}/{attempts} "
                f"({last_detail}) — retrying in {int(sleep_s)}s…",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(sleep_s)
    return 0, {
        "_socket_error": {
            "kind": last_kind,
            "detail": last_detail,
            "operation": retry_label,
            "timeout_s": timeout,
            "attempts": attempts,
        }
    }


def post_json(
    url: str,
    token: str,
    payload: Any,
    timeout: float = 60.0,
    retry_label: str = "post_json",
    max_retries: int = 1,
) -> tuple[int, dict]:
    status, raw = request_with_retry(
        url=url,
        method="POST",
        token=token,
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        timeout=timeout,
        max_retries=max_retries,
        retry_label=retry_label,
    )
    # Retry-exhaustion sentinel: status=0 with a dict body already shaped for
    # decode_socket_error(). Pass it through untouched.
    if status == 0 and isinstance(raw, dict):
        return status, raw
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode("utf-8", errors="replace")}


# ─── auth error decoding ──────────────────────────────────────────────────


def decode_auth_error(status: int, body: dict) -> str:
    """
    Turn an unhelpful "401 unauthorized" into something the user can act on.

    The Mikoshi server includes a hint in the response body for most
    auth failures — token revoked, account disabled, wrong account id,
    etc. Parse the common shapes; fall back to a generic message when
    the body doesn't match any pattern we know.

    Returns a multi-line string suitable for printing to stderr.
    """
    blob = json.dumps(body).lower() if isinstance(body, dict) else str(body).lower()

    base = f"[ERROR] Server returned {status}"

    if status == 401:
        if "token" in blob and ("expir" in blob or "revok" in blob):
            return (
                f"{base}: token rejected (expired or revoked).\n"
                "  Regenerate a new token in Mikoshi: /accounts/<id>/ingestion → 'Regenerate'.\n"
                "  Then update MIKOSHI_TOKEN in ~/.mikoshi-ingest.conf and re-run."
            )
        if "account" in blob and ("disabled" in blob or "suspend" in blob):
            return (
                f"{base}: account disabled server-side.\n"
                "  Re-enable at /accounts/<id> in Mikoshi, then re-run."
            )
        if "invalid" in blob or "unauthorized" in blob or not blob.strip():
            return (
                f"{base}: token rejected.\n"
                "  Common causes:\n"
                "    1. MIKOSHI_TOKEN in ~/.mikoshi-ingest.conf is wrong / blank.\n"
                "    2. The Mikoshi account behind this token has been disabled.\n"
                "    3. Server rotated tokens — regenerate at /accounts/<id>/ingestion.\n"
                "  Test with: ./mikoshi-whatsapp.sh test-auth"
            )
        return f"{base}: {body}"

    if status == 403:
        return (
            f"{base}: forbidden.\n"
            "  Token is valid but this account doesn't have ingest permission.\n"
            "  Check /accounts/<id>/ingestion settings in Mikoshi."
        )
    if status == 404:
        return (
            f"{base}: endpoint not found.\n"
            "  Either MIKOSHI_URL is wrong, or your Mikoshi server is too old to\n"
            "  speak the current ingest API. Verify by opening MIKOSHI_URL in a browser."
        )
    if status == 413:
        return (
            f"{base}: payload too large.\n"
            "  The manifest exceeded the server's body-size limit. Split the sync\n"
            "  by running smaller scopes (favorites, or one chat at a time)."
        )
    if 500 <= status <= 599:
        return (
            f"{base}: server error.\n"
            f"  Body: {body}\n"
            "  Check Mikoshi logs (typically /var/log/mikoshi/) and retry."
        )
    return f"{base}: {body}"


def decode_socket_error(body: dict) -> str:
    """Render a friendly message from a request_with_retry exhaustion sentinel.

    `body` is the dict returned in the (0, body) tuple — i.e. it contains a
    `_socket_error` key with kind/detail/operation/timeout_s/attempts.
    """
    info = body.get("_socket_error", {}) if isinstance(body, dict) else {}
    kind = info.get("kind", "unknown")
    detail = info.get("detail", "")
    operation = info.get("operation", "request")
    timeout_s = info.get("timeout_s", 0)
    attempts = info.get("attempts", 1)

    base = f"[ERROR] {operation} failed after {attempts} attempt(s): {kind}"
    if detail:
        base += f" ({detail})"

    if operation == "manifest" and kind in ("broken_pipe", "connection_reset"):
        return (
            f"{base}\n"
            "  The Mikoshi server (or a proxy in front of it) closed the connection mid-upload.\n"
            "  Most often a body-size limit on the server. Workarounds:\n"
            "    1. Narrow the scope (TUI → Sync → per-chat).\n"
            "    2. Lower MIKOSHI_BATCH_BYTES (default 50MB) to force smaller batches.\n"
            "    3. On jetson, raise maxRequestBodySize on Bun.serve()."
        )
    if operation == "manifest" and kind == "timeout":
        return (
            f"{base}\n"
            f"  Server didn't respond within {int(timeout_s)}s while uploading the manifest.\n"
            "  Check Mikoshi logs (e.g. `journalctl --user -u mikoshi` on jetson), then retry."
        )
    if operation == "commit" and kind == "timeout":
        return (
            f"{base}\n"
            f"  Server accepted manifest+media but /commit didn't reply within {int(timeout_s)}s.\n"
            "  /commit is idempotent — re-running the sync is safe.\n"
            "  Most often the server is wedged on a large DB write; check Mikoshi logs."
        )
    if operation == "commit" and kind in ("broken_pipe", "connection_reset"):
        return (
            f"{base}\n"
            "  Connection dropped while waiting for the /commit response.\n"
            "  The commit may have completed server-side — re-running the sync is safe (idempotent).\n"
            "  If it persists, the server may have crashed: check `pgrep -af bun` on jetson."
        )
    return (
        f"{base}\n"
        "  Retry the sync. If it persists, check Mikoshi server status."
    )


# ─── commit-wait heartbeat ────────────────────────────────────────────────


class CommitHeartbeat:
    """Context manager that prints a heartbeat line while a long /commit is in flight.

    The /commit endpoint can block for tens of seconds (or minutes, pre
    server-side transaction fix) on a large push. Without this, the user
    just sees "[INFO] committing push" and an apparent hang. We start a
    daemon thread that prints elapsed seconds every `interval_s` until
    the body of the `with` block returns.
    """

    def __init__(self, label: str = "commit", interval_s: float = 30.0):
        self.label = label
        self.interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start: float = 0.0

    def __enter__(self) -> "CommitHeartbeat":
        self._start = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"{self.label}-heartbeat"
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            elapsed = int(time.monotonic() - self._start)
            print(
                f"[INFO] still waiting for {self.label} response… {elapsed}s elapsed",
                flush=True,
            )

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        elapsed = time.monotonic() - self._start
        if elapsed > self.interval:
            print(
                f"[INFO] {self.label} response received after {int(elapsed)}s",
                flush=True,
            )


# ─── media upload ─────────────────────────────────────────────────────────


def upload_media(
    url: str,
    token: str,
    sha256: str,
    file_path: Path,
    max_retries: int = 4,
) -> tuple[int, dict]:
    body = file_path.read_bytes()
    ext = file_path.suffix.lower()
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }.get(ext, "application/octet-stream")

    backoff = 1.0
    last_status = 0
    last_body: dict = {}
    for attempt in range(max_retries):
        status, raw = http_request(
            url=f"{url}/api/ingest/v1/media/{sha256}",
            method="POST",
            token=token,
            body=body,
            content_type=content_type,
            timeout=120.0,
        )
        if status == 204:
            return status, {}
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw.decode("utf-8", errors="replace")}
        last_status, last_body = status, parsed
        # Non-retryable: 4xx other than 429.
        if 400 <= status < 500 and status != 429:
            return status, parsed
        time.sleep(backoff)
        backoff *= 2
    return last_status, last_body


# ─── auth probe (for `mikoshi-whatsapp.sh test-auth` and TUI Setup screen) ─


def test_auth(url: str, token: str, timeout: float = 5.0) -> tuple[bool, str]:
    """
    Verify a (URL, token) pair without pushing anything.

    Strategy: try `GET /api/ingest/v1/cursor` (current Mikoshi). On
    200/204 auth is good. On 401/403, decode and return the friendly
    message. On 404, the auth endpoint doesn't exist on this older
    Mikoshi — try a HEAD against the manifest endpoint as a fallback
    (with an empty body it'll 400 or 405 if auth was OK, 401 if not).
    """
    if not url or not token:
        return False, "MIKOSHI_URL and MIKOSHI_TOKEN are not both set."
    full = url.rstrip("/") + "/api/ingest/v1/cursor"
    try:
        status, raw = http_request(full, method="GET", token=token, timeout=timeout)
    except Exception as e:  # pragma: no cover — network errors
        return False, f"network error: {e}"
    if status in (200, 204):
        return True, f"OK — {url} accepts this token (cursor endpoint reachable)."
    if status == 404:
        # Old server. Try the manifest endpoint with an empty POST to gauge auth alone.
        try:
            status2, raw2 = http_request(
                url.rstrip("/") + "/api/ingest/v1/manifest",
                method="POST", token=token,
                body=b"{}", content_type="application/json", timeout=timeout,
            )
        except Exception as e:  # pragma: no cover
            return False, f"network error: {e}"
        if status2 in (400, 405, 422):
            return True, f"OK — {url} accepts this token (server is pre-M2; cursor endpoint missing)."
        body: dict = {}
        try:
            body = json.loads(raw2) if raw2 else {}
        except json.JSONDecodeError:
            body = {"_raw": raw2.decode("utf-8", errors="replace")}
        return False, decode_auth_error(status2, body)
    body = {}
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"_raw": raw.decode("utf-8", errors="replace")}
    return False, decode_auth_error(status, body)


# ─── manifest batching ────────────────────────────────────────────────────


DEFAULT_BATCH_BYTES = 50 * 1024 * 1024  # 50 MiB serialized per batch
DEFAULT_BATCH_MESSAGES = 50_000         # message count guard per batch


def split_manifest_by_size(
    manifest: dict,
    max_bytes: int,
    max_messages: int,
) -> list[dict]:
    """Greedy partition of `manifest['chats']` into sub-manifests.

    Each sub-manifest inherits the top-level fields of `manifest` (schema_version,
    account metadata, etc.) and carries a subset of `chats`. The greedy packer
    flushes the current batch when adding the next chat would exceed either
    `max_bytes` (serialized size) or `max_messages` (message count). A single
    chat that exceeds either threshold on its own is emitted as a one-chat
    batch — we never split inside a chat.

    Pass `max_bytes <= 0` or `max_messages <= 0` to disable batching (returns
    a single-entry list with the original manifest).
    """
    chats = manifest.get("chats") or []
    if not chats or max_bytes <= 0 or max_messages <= 0:
        return [manifest]

    def _sub(subset: list[dict]) -> dict:
        out = {k: v for k, v in manifest.items() if k != "chats"}
        out["chats"] = subset
        return out

    batches: list[dict] = []
    cur: list[dict] = []
    cur_bytes = 0
    cur_msgs = 0
    for chat in chats:
        size = len(json.dumps(chat).encode("utf-8"))
        msgs = len(chat.get("messages") or [])
        if cur and (cur_bytes + size > max_bytes or cur_msgs + msgs > max_messages):
            batches.append(_sub(cur))
            cur, cur_bytes, cur_msgs = [], 0, 0
        cur.append(chat)
        cur_bytes += size
        cur_msgs += msgs
    if cur:
        batches.append(_sub(cur))
    return batches or [manifest]


def cursors_from_manifest_dict(manifest: dict) -> dict[str, dict[str, str]]:
    """Derive a `committed_cursors`-shaped dict from a manifest in memory.

    Used as a fallback when batching against an old Mikoshi server that doesn't
    echo `committed_cursors`. Mirrors the logic in
    `pipeline_state.update_cache_from_extraction_fallback` but operates on the
    in-memory sub-manifest dict rather than a file on disk — since batched
    sub-manifests aren't persisted.
    """
    cursors: dict[str, dict[str, str]] = {}
    for chat in manifest.get("chats", []):
        jid = chat.get("jid")
        if not jid:
            continue
        best_ts: str | None = None
        best_ext: str | None = None
        for msg in chat.get("messages", []):
            ts = msg.get("timestamp")
            if ts is None:
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_ext = msg.get("external_id")
        if best_ts is None or best_ext is None:
            continue
        cursors[jid] = {"ts": best_ts, "external_id": best_ext}
    return cursors


# ─── main push flow ───────────────────────────────────────────────────────


def push_one_batch(
    *,
    url: str,
    token: str,
    manifest: dict,
    attachments_dir: Path,
    concurrency: int,
    state_file: Path,
    no_cursor_write: bool,
    batch_label: str = "",
    heartbeat_interval: float = 30.0,
    commit_timeout_s: float = 1800.0,
    manifest_timeout_s: float = 120.0,
    max_retries: int = 1,
    fallback_manifest_path: Path | None = None,
) -> int:
    """Run one manifest → media → commit cycle on a (sub-)manifest.

    Returns 0 on success, 1 on non-recoverable failure (decoded message
    already printed to stderr). `fallback_manifest_path` is used only when
    the server does NOT echo `committed_cursors` AND this batch is the full
    on-disk manifest — otherwise we derive cursors from the in-memory dict.
    """
    chats_count = len(manifest.get("chats", []))
    prefix = f"{batch_label} " if batch_label else ""

    print(f"[INFO] {prefix}Submitting manifest ({chats_count} chats) → {url}")
    status, body = post_json(
        f"{url}/api/ingest/v1/manifest",
        token,
        manifest,
        timeout=manifest_timeout_s,
        retry_label="manifest",
        max_retries=max_retries,
    )
    if status == 0 and isinstance(body, dict) and "_socket_error" in body:
        print(decode_socket_error(body), file=sys.stderr)
        return 1
    if status != 200:
        print(decode_auth_error(status, body), file=sys.stderr)
        return 1

    push_id = body.get("push_id")
    needs_media: list[str] = body.get("needs_media", [])
    rejected: list[dict] = body.get("rejected_messages", [])
    if not push_id:
        print(f"[ERROR] response missing push_id: {body}", file=sys.stderr)
        return 1

    print(f"[INFO] {prefix}push_id={push_id} needs_media={len(needs_media)} rejected={len(rejected)}")
    if rejected:
        for r in rejected[:5]:
            print(f"  rejected: {r}")
        if len(rejected) > 5:
            print(f"  …and {len(rejected) - 5} more")

    if needs_media:
        print(f"[INFO] {prefix}uploading {len(needs_media)} media files (concurrency={concurrency})")
        files_by_hash: dict[str, Path] = {}
        for p in attachments_dir.iterdir():
            stem = p.stem
            if len(stem) == 64:
                files_by_hash[stem] = p

        missing_local: list[str] = []
        successes = 0
        failures: list[tuple[str, int, dict]] = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {}
            for sha in needs_media:
                path = files_by_hash.get(sha)
                if not path:
                    missing_local.append(sha)
                    continue
                futures[pool.submit(upload_media, url, token, sha, path)] = sha
            for fut in as_completed(futures):
                sha = futures[fut]
                m_status, m_body = fut.result()
                if m_status == 204:
                    successes += 1
                else:
                    failures.append((sha, m_status, m_body))

        print(
            f"[INFO] {prefix}media uploads: {successes} ok, "
            f"{len(failures)} failed, {len(missing_local)} not found locally"
        )
        if missing_local:
            print(
                "[ERROR] some media files referenced by the manifest are not on disk —"
                " did you rotate attachments/ ?",
                file=sys.stderr,
            )
            for s in missing_local[:5]:
                print(f"  missing: {s}", file=sys.stderr)
            return 1
        if failures:
            for sha, m_status, m_body in failures[:5]:
                print(f"[ERROR] {sha[:12]}…: {m_status} {m_body}", file=sys.stderr)
            return 1

    print(f"[INFO] {prefix}committing push")
    with CommitHeartbeat(label="commit", interval_s=heartbeat_interval):
        status, body = post_json(
            f"{url}/api/ingest/v1/commit",
            token,
            {"push_id": push_id},
            timeout=commit_timeout_s,
            retry_label="commit",
            max_retries=max_retries,
        )
    if status == 0 and isinstance(body, dict) and "_socket_error" in body:
        print(decode_socket_error(body), file=sys.stderr)
        return 1
    if status != 200:
        print(decode_auth_error(status, body), file=sys.stderr)
        return 1
    print(f"[OK] {prefix}commit succeeded — stats: {body.get('stats')}")

    # --- the one place cursors are written ---
    #
    # New Mikoshi (M2+) echoes `committed_cursors: {jid: {ts, external_id}}` in
    # the commit response — authoritative. Old Mikoshi omits the block; we then
    # derive cursors locally from the (sub-)manifest we just pushed. Either
    # way, this write happens ONLY after commit returned 200.
    if not no_cursor_write:
        committed_cursors = body.get("committed_cursors") or {}
        if committed_cursors:
            pipeline_state.update_cache_from_commit(
                state_file=state_file,
                server_url=url,
                push_id=push_id,
                committed_cursors=committed_cursors,
            )
            print(
                f"[OK] {prefix}cursor cache updated from server "
                f"(committed_cursors for {len(committed_cursors)} chats)"
            )
        elif fallback_manifest_path is not None:
            # Single-batch / whole-file path: reuse the file-based fallback
            # (preserves SOURCE_EXTRACTED_OFFLINE labeling that drift detection
            # uses to know it should re-verify).
            pipeline_state.update_cache_from_extraction_fallback(
                state_file=state_file,
                manifest_path=fallback_manifest_path,
                server_url=url,
                push_id=push_id,
            )
            print("[OK] cursor cache updated from manifest (server didn't echo committed_cursors — old Mikoshi?)")
        else:
            # Batched path against old Mikoshi: compute in memory.
            derived = cursors_from_manifest_dict(manifest)
            pipeline_state.update_cache_from_commit(
                state_file=state_file,
                server_url=url,
                push_id=push_id,
                committed_cursors=derived,
            )
            print(
                f"[OK] {prefix}cursor cache updated from sub-manifest "
                f"(derived {len(derived)} chats; old Mikoshi)"
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Push a WhatsApp export to a Mikoshi server.")
    parser.add_argument("--manifest", required=True, type=Path, help="Path to export JSON (schema_version 1.2).")
    parser.add_argument(
        "--attachments-dir",
        required=True,
        type=Path,
        help="Directory holding sha256-keyed attachments referenced by the manifest.",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--url", help="Override MIKOSHI_URL.")
    parser.add_argument("--token", help="Override MIKOSHI_TOKEN.")
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(__file__).parent / ".sync_state.json",
        help="Cursor cache file to update on commit success (default: .sync_state.json next to this script).",
    )
    parser.add_argument(
        "--no-cursor-write",
        action="store_true",
        help="Skip writing the cursor cache even on success. For dry-runs and tests.",
    )
    parser.add_argument(
        "--batch-bytes",
        type=int,
        default=int(os.environ.get("MIKOSHI_BATCH_BYTES", DEFAULT_BATCH_BYTES)),
        help=(
            "Soft cap on serialized bytes per batch (default 50MB). "
            "0 disables batching (push the whole manifest in one request)."
        ),
    )
    parser.add_argument(
        "--batch-messages",
        type=int,
        default=int(os.environ.get("MIKOSHI_BATCH_MESSAGES", DEFAULT_BATCH_MESSAGES)),
        help="Soft cap on messages per batch (default 50000). 0 disables batching.",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=0,
        help=(
            "Abort cleanly between batches once this many seconds have elapsed. "
            "0 (default) = no cap. Useful for the LaunchAgent daily sync."
        ),
    )
    parser.add_argument(
        "--commit-timeout",
        type=float,
        default=1800.0,
        help="Per-request timeout (seconds) for /commit. Default 1800 (30 min).",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=30.0,
        help="How often to log '[INFO] still waiting for commit response…' during /commit.",
    )
    args = parser.parse_args()

    cfg = load_config()
    url = args.url or cfg.get("MIKOSHI_URL", "").rstrip("/")
    token = args.token or cfg.get("MIKOSHI_TOKEN", "")
    if not url or not token:
        print("[ERROR] MIKOSHI_URL and MIKOSHI_TOKEN must be set (env or ~/.mikoshi-ingest.conf)", file=sys.stderr)
        return 2

    if not args.manifest.exists():
        print(f"[ERROR] Manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    if not args.attachments_dir.exists():
        print(f"[ERROR] Attachments dir not found: {args.attachments_dir}", file=sys.stderr)
        return 2

    manifest = json.loads(args.manifest.read_text())
    if manifest.get("schema_version") != "1.2":
        print(
            f"[ERROR] schema_version {manifest.get('schema_version')!r} not accepted by the Mikoshi REST API."
            f" Re-export with the bundled extract_messages.py (1.2).",
            file=sys.stderr,
        )
        return 2

    batches = split_manifest_by_size(manifest, args.batch_bytes, args.batch_messages)
    is_single_batch = len(batches) == 1
    if not is_single_batch:
        print(
            f"[INFO] Splitting manifest into {len(batches)} batches "
            f"(cap: {args.batch_bytes // (1024 * 1024)}MB / {args.batch_messages} msgs per batch)"
        )

    start_time = time.monotonic()

    for idx, batch in enumerate(batches, start=1):
        if args.max_runtime_seconds > 0:
            elapsed = time.monotonic() - start_time
            if elapsed >= args.max_runtime_seconds:
                committed = idx - 1
                print(
                    f"[ERROR] --max-runtime-seconds elapsed after batch {committed}/{len(batches)} "
                    f"({int(elapsed)}s ≥ {args.max_runtime_seconds}s) — exiting clean",
                    file=sys.stderr,
                )
                return 4

        label = "" if is_single_batch else f"[batch {idx}/{len(batches)}]"
        rc = push_one_batch(
            url=url,
            token=token,
            manifest=batch,
            attachments_dir=args.attachments_dir,
            concurrency=args.concurrency,
            state_file=args.state_file,
            no_cursor_write=args.no_cursor_write,
            batch_label=label,
            heartbeat_interval=args.heartbeat_interval,
            commit_timeout_s=args.commit_timeout,
            fallback_manifest_path=args.manifest if is_single_batch else None,
        )
        if rc != 0:
            if not is_single_batch:
                print(
                    f"[ERROR] batch {idx}/{len(batches)} failed; "
                    f"{idx - 1}/{len(batches)} batches already committed (cursors persisted).",
                    file=sys.stderr,
                )
            return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
