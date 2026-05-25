#!/usr/bin/env python3
"""
Push a WhatsApp export to a Mikoshi server via the REST ingestion API.

Three-step protocol (content-addressed, idempotent):
  1. POST /api/ingest/v1/manifest  → server returns push_id + needs_media[]
  2. POST /api/ingest/v1/media/<sha256> (raw bytes) for each missing hash
  3. POST /api/ingest/v1/commit { push_id } → server persists messages +
     attachments and queues them for scan-based memory extraction.

Configuration via env or ~/.mikoshi-ingest.conf:
  MIKOSHI_URL    e.g. https://mikoshi.example.com  (NO trailing slash)
  MIKOSHI_TOKEN  bearer token (generated from /accounts/<id>/ingestion in Mikoshi)

Re-pushing the same export is safe: the server keys idempotency on
(account_id, external_id) — duplicates are silently skipped.
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


def upload_media(
    url: str,
    token: str,
    sha256: str,
    file_path: Path,
    max_retries: int = 4,
) -> tuple[int, dict]:
    body = file_path.read_bytes()
    # Guess content-type from extension; the server recomputes the sha256 and
    # validates against the path segment, so a wrong mime here only matters
    # for filter eligibility.
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
        print(f"[ERROR] manifest POST failed: {status} {body}", file=sys.stderr)
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
        # Map sha256 → file path. The extractor names files <sha256><ext>.
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
        print(f"[ERROR] commit failed: {status} {body}", file=sys.stderr)
        return 1
    print(f"[OK] commit succeeded — stats: {body.get('stats')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
