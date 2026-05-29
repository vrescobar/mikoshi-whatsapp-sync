# FIX_SYNC.md — Client-side hardening for Mikoshi REST push

## Context

The Mac client has hit two Phase-5 failures against `http://jetson:7777`
within 24 h. Diagnosis confirms both are server-side (see
`~/projects/mikoshi/FIX_SYNC.md` on jetson — synchronous untransacted
commit loop + Bun.serve defaults of 128 MiB body / 10 s idle).

Even once the server is fixed, the client is too brittle: any future
transient (network blip, server restart, large backfill) currently
nukes the whole pipeline. This plan toughens the push client so the
*next* failure is recoverable instead of fatal.

## Today's failure recap

| Attempt | Endpoint | Error | Wall-clock |
|---|---|---|---|
| 2026-05-28 02:34 | `POST /api/ingest/v1/manifest` (142 MB body) | `BrokenPipeError` | ~10 s |
| 2026-05-29 15:15 | `POST /api/ingest/v1/commit` (100 k msgs) | `TimeoutError` on response read | ~13 min (5 min media + 5 min commit + retries) |

Both surfaced as a raw Python traceback. The user has no actionable signal beyond "look at the log."

## Recommended changes (in priority order)

All changes live in `push_via_api.py` unless noted. Keep the three-step
protocol (manifest → media → commit) intact — only the I/O layer changes.

### 1. **Raise the `/commit` timeout and add a single retry.**

`push_via_api.py:384` currently has `timeout=300.0` on the commit POST.
With a 100 k-message commit that's catastrophically tight. After the
server fix this becomes a non-issue, but defense-in-depth is cheap.

- Bump `/commit` timeout to **1800 s** (30 min) — covers worst-case 1 M-message backfill on the Jetson even with the server fix applied.
- Wrap the commit POST in one retry with 5 s sleep when the error is `TimeoutError`, `BrokenPipeError`, `ConnectionResetError`, or `URLError` wrapping any of those. Do **not** retry on HTTP non-2xx (those are deterministic — auth, schema, missing media).
- The retry is safe because `/commit` is idempotent server-side: if the first attempt actually completed, the second receives `"already committed"` and the cursor cache still advances correctly.

### 2. **Translate low-level socket errors into actionable messages.**

`push_via_api.py:148-160` already does this for HTTP statuses (401/404/413/5xx). Extend `decode_auth_error()` (or add a sibling `decode_socket_error()`) so that:

- `BrokenPipeError` → "Server (or proxy) rejected the request mid-upload. Most often a body-size limit on the Mikoshi server. Try a narrower scope (`--chat <jid>`) or raise `maxRequestBodySize` on `Bun.serve()`."
- `TimeoutError` during `/commit` → "Server accepted the manifest and media but `/commit` did not respond in N seconds. Check Mikoshi server logs — most often the commit is wedged on a long DB write. Idempotent: re-running the sync is safe."
- `ConnectionResetError` → similar to BrokenPipe, plus "may also indicate the server crashed during the request — check `pgrep -af bun` on the server host."

Hook these into the top-level handler in `main()` so the user sees one clear line instead of a Python traceback.

### 3. **Auto-split large manifests by chat before posting.**

Today's failing scope had 85 chats / 100 k messages → one POST. If the manifest exceeds a threshold (~50 MB serialized OR ~50 k messages — whichever first), split it:

- Group chats into batches whose total serialized size stays under the threshold.
- Run the full three-step protocol per batch (manifest → media → commit).
- Cursors advance per-batch on commit success — partial progress is preserved if a later batch fails.

This complements the server-side fix; it also unblocks the 1 M-message full backfill use case without depending on async-commit on the server.

Implementation hint: the existing `pipeline_state.update_cache_from_commit()` is already per-chat (`committed_cursors: Record<jid, ...>`), so splitting the manifest doesn't break the cursor model — it just generates more `commit` calls.

### 4. **Surface progress during long `/commit` waits.**

Right now the client prints `[INFO] committing push` and then blocks
silently for minutes. Add a heartbeat: a background thread that emits
`[INFO] still waiting for commit response… (Ns elapsed)` every 30 s
while the main thread is in `post_json` for `/commit`. The user (and
the launchd job) can then tell "stuck" from "working."

Minimal version: a `threading.Timer` that prints elapsed seconds until
the outer call returns.

### 5. **(Stretch)** Plumb a `--strict-timeout` flag.

When the LaunchAgent runs the daily 03:00 sync, you don't want it
blocking for 30 min on a wedged server. Add `--max-runtime-seconds`
(default unlimited, scheduler sets it to e.g. 900) that aborts the
whole pipeline with a clean exit code if exceeded. The launchd unit's
`StandardErrorPath` will then carry a clear failure instead of a
stuck process.

## Critical files

- `push_via_api.py:61-92` — HTTP helpers (`http_request`, `post_json`). Add the socket-error retry + decode here.
- `push_via_api.py:317` — manifest POST. After (3), this becomes a loop over batches.
- `push_via_api.py:384` — commit POST. Apply (1) + (4) here.
- `push_via_api.py:98-160` — `decode_auth_error`. Extend with the socket-level cases for (2).
- `pipeline_state.update_cache_from_commit()` — no changes needed for (3); already per-chat.
- `run_pipeline.sh` Phase 5 invocation — no changes; all batching stays inside `push_via_api.py`.

## What I will NOT do here

- **Touch the schema or the three-step protocol.** The server contract (`manifest`/`media`/`commit`) is fine; the bug is in I/O resilience around it.
- **Hide errors.** All retries log clearly that they happened and surface the final status.
- **Pre-commit cursor writes.** The existing invariant — only `commit` success moves cursors — stays. Batching from (3) just means smaller, more frequent commits.

## Verification

After (1) and (2):

- Kill the Mikoshi server briefly during a sync; expect one `[WARN] retrying /commit after TimeoutError…` then either success or a one-line decoded error, not a Python traceback.
- Run today's failing scope (Mac-live, favorites, ~100 k msgs) once the server fix from `~/projects/mikoshi/FIX_SYNC.md` is deployed; expect `[OK] commit succeeded` in seconds.

After (3):

- Run a full-history backfill (`--scope all`, both sources, ~1 M messages) and confirm it completes as a sequence of N batched commits with cursors advancing per-batch.
- Kill the server between batches; re-run and confirm only the un-committed batches are retried.

After (4):

- Sync where the server is intentionally slow (e.g. before applying the server-side transaction fix); confirm the user sees `[INFO] still waiting for commit response… 30s elapsed` and isn't left guessing.

## Ordering against server-side work

- **Land server FIX_SYNC.md (1) + (2) first** — that single transaction wrap removes the timeout class of failure entirely. After that, (1) and (2) here become defense-in-depth, (3) becomes a backfill-only feature, (4) is purely UX.
- **Land (1) and (2) here together** as a small PR; they don't depend on the server fix and are useful even alone.
- **Defer (3), (4), (5)** until the first three are merged and we've observed at least one successful sync end-to-end.

## Implementation status (2026-05-29)

All five items have landed in `push_via_api.py` along with
`tests/test_push_via_api_resilience.py` (25 tests; full suite 391 green).

| Item | Shipped as |
|---|---|
| (1) commit timeout + retry | `request_with_retry()` wraps `http_request`; `post_json` routes through it; manifest gets `timeout=120s` + 1 retry, `/commit` gets `timeout=1800s` + 1 retry. On exhaustion returns the `(0, {"_socket_error": …})` sentinel. |
| (2) socket error decoder | `decode_socket_error()` sibling of `decode_auth_error()`, dispatched by `(operation, kind)` so the user gets a one-line actionable message instead of a Python traceback. |
| (3) auto-split | `split_manifest_by_size()` + refactor of `main()` into `push_one_batch()` looped over batches. Defaults: 50 MiB or 50 k messages per batch (override via `--batch-bytes` / `--batch-messages` / env `MIKOSHI_BATCH_*`; set either to `0` to disable). Cursors advance per-batch on each successful `/commit`. |
| (4) heartbeat | `CommitHeartbeat` context manager wraps the `/commit` POST. Default 30 s interval, override with `--heartbeat-interval`. Daemon thread; never blocks shutdown. |
| (5) runtime cap | `--max-runtime-seconds N` (default `0` = no cap). Checked at each batch boundary — never aborts mid-commit. Returns exit code **4** on expiry with an explicit `[ERROR] --max-runtime-seconds elapsed after batch N/M …` line. |

The three-step protocol, schema, and `pipeline_state` module are untouched.
`upload_media`'s existing retry is left as-is (already handled in its own loop).
