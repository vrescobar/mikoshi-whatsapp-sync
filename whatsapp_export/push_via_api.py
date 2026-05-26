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
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pipeline_state


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


def post_json(url: str, token: str, payload: Any, timeout: float = 60.0) -> tuple[int, dict]:
    status, raw = http_request(
        url=url,
        method="POST",
        token=token,
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        timeout=timeout,
    )
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

    Strategy: try `GET /api/ingest/v1/cursors` (M2 endpoint). On 200/204
    auth is good. On 401/403, decode and return the friendly message.
    On 404, the auth endpoint doesn't exist on this old Mikoshi — try
    a HEAD against the manifest endpoint as a fallback (with an empty
    body it'll 400 or 405 if auth was OK, 401 if not).
    """
    if not url or not token:
        return False, "MIKOSHI_URL and MIKOSHI_TOKEN are not both set."
    full = url.rstrip("/") + "/api/ingest/v1/cursors"
    try:
        status, raw = http_request(full, method="GET", token=token, timeout=timeout)
    except Exception as e:  # pragma: no cover — network errors
        return False, f"network error: {e}"
    if status in (200, 204):
        return True, f"OK — {url} accepts this token (cursors endpoint reachable)."
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
            return True, f"OK — {url} accepts this token (server is pre-M2; cursors endpoint missing)."
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


# ─── main push flow ───────────────────────────────────────────────────────


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

    print(f"[INFO] Submitting manifest ({len(manifest.get('chats', []))} chats) → {url}")
    status, body = post_json(f"{url}/api/ingest/v1/manifest", token, manifest, timeout=120.0)
    if status != 200:
        # The most-common failure mode is auth; surface a decoded hint instead
        # of the raw body. The original message dumped to stdout was useless
        # ("[ERROR] manifest POST failed: 401 {...}") and forced the user to
        # debug blind. See REDESIGN.md pain point #8.
        print(decode_auth_error(status, body), file=sys.stderr)
        return 1

    push_id = body.get("push_id")
    needs_media: list[str] = body.get("needs_media", [])
    rejected: list[dict] = body.get("rejected_messages", [])
    if not push_id:
        print(f"[ERROR] response missing push_id: {body}", file=sys.stderr)
        return 1

    print(f"[INFO] push_id={push_id} needs_media={len(needs_media)} rejected={len(rejected)}")
    if rejected:
        for r in rejected[:5]:
            print(f"  rejected: {r}")
        if len(rejected) > 5:
            print(f"  …and {len(rejected) - 5} more")

    if needs_media:
        print(f"[INFO] uploading {len(needs_media)} media files (concurrency={args.concurrency})")
        attachments_root = args.attachments_dir
        files_by_hash: dict[str, Path] = {}
        for p in attachments_root.iterdir():
            stem = p.stem
            if len(stem) == 64:
                files_by_hash[stem] = p

        missing_local: list[str] = []
        successes = 0
        failures: list[tuple[str, int, dict]] = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {}
            for sha in needs_media:
                path = files_by_hash.get(sha)
                if not path:
                    missing_local.append(sha)
                    continue
                futures[pool.submit(upload_media, url, token, sha, path)] = sha
            for fut in as_completed(futures):
                sha = futures[fut]
                status, body = fut.result()
                if status == 204:
                    successes += 1
                else:
                    failures.append((sha, status, body))

        print(f"[INFO] media uploads: {successes} ok, {len(failures)} failed, {len(missing_local)} not found locally")
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
            for sha, status, body in failures[:5]:
                print(f"[ERROR] {sha[:12]}…: {status} {body}", file=sys.stderr)
            return 1

    print("[INFO] committing push")
    status, body = post_json(f"{url}/api/ingest/v1/commit", token, {"push_id": push_id}, timeout=300.0)
    if status != 200:
        print(decode_auth_error(status, body), file=sys.stderr)
        return 1
    print(f"[OK] commit succeeded — stats: {body.get('stats')}")

    # --- the one place cursors are written ---
    #
    # Two paths:
    #   1. New Mikoshi (M2+): the commit response includes
    #      `committed_cursors: {jid: {ts, external_id}}`. Authoritative.
    #   2. Old Mikoshi: no committed_cursors → derive cursors from the
    #      manifest we just successfully pushed. The values are slightly
    #      weaker (source=extracted-offline) so drift detection will
    #      re-verify them against the server next time it can.
    #
    # Either way, this write happens ONLY after commit returned 200.
    # A 401/5xx earlier in this function returns before we get here.
    if not args.no_cursor_write:
        committed_cursors = body.get("committed_cursors") or {}
        if committed_cursors:
            pipeline_state.update_cache_from_commit(
                state_file=args.state_file,
                server_url=url,
                push_id=push_id,
                committed_cursors=committed_cursors,
            )
            print(f"[OK] cursor cache updated from server (committed_cursors for {len(committed_cursors)} chats)")
        else:
            pipeline_state.update_cache_from_extraction_fallback(
                state_file=args.state_file,
                manifest_path=args.manifest,
                server_url=url,
                push_id=push_id,
            )
            print("[OK] cursor cache updated from manifest (server didn't echo committed_cursors — old Mikoshi?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
