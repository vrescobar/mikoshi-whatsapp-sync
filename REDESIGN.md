# REDESIGN — mikoshi-whatsapp-sync

## Context — why this redesign

The pipeline works end-to-end but has accreted enough surface area that a
recent multi-hour debug session exposed a structural footgun: local
cursors in `.sync_state.json` advance the moment `extract_messages.py`
finishes, *before* the push to Mikoshi is even attempted. A push that
401s leaves the client believing data is synced while the server has
nothing — and the next "sync favorites" returns "0 messages" with no
warning. The user lost trust in the TUI status display.

Around that core bug, several smaller bugs and UX gaps reinforce each
other: 14 unstructured TUI actions, misleading labels (Full was
incremental, "0 messages" looks like a failure), tightly-coupled phases
(`secure_cleanup` shredding the very file Phase 4 needs), two divergent
entry points (TUI is smart, cron isn't), and no way to see what's on the
server short of `ssh jetson && sqlite3`.

This redesign is not a rewrite. Most of the codebase is sound: selective
decrypt, schema validation, Keychain integration, sha256-keyed
attachments, server-side idempotency on `external_id`, the external-SSD
support. The change is targeted at **two things**:

1. Make cursor advancement *physically incapable* of getting ahead of
   the server (architectural — §4).
2. Reorganize the TUI around the user's intent, not the pipeline's
   phases (UX — §5).

Everything else follows from those two.

---

## 1. Executive summary

Two structural fixes carry the redesign:

**Server becomes source of truth for "what's synced."** Cursors live
server-side (already do — `ingestion_cursor`) and are advanced *only*
inside the `commit` request handler. The client treats
`.sync_state.json` as a local cache mirrored from the server's response
to `commit`. Before extraction the client asks the server "where are
you?" and works from that answer, not from local state. Failed push =
no cursor movement, anywhere. Deleting `.sync_state.json` becomes safe.

**TUI reorganized around intent, not phases.** Five top-level actions
(Sync, Inspect, Favorites, Setup, Tools) replace the 14-item flat list.
A persistent header shows `iPhone · Backup · Decrypt · Server · Drift`
state so the user always knows what's true before clicking.
"Sync" auto-detects source (iPhone vs cached backup) and shows a
plan ("will push 1,247 new messages across 3 chats") before running.

Supporting changes: split Phase 3 into DB-only and media decrypt;
demote `secure_cleanup` to opt-in (since PRESERVE has been the default
since `caaa140`); make `mikoshi-whatsapp.sh sync` share the smart
detection with the TUI so cron survives an unplugged iPhone; add a
401-decoder so auth failures suggest the right fix.

Minimal new code. No new dependencies. One small server-side endpoint
(`GET /api/ingest/v1/cursors`) ships as a separate Mikoshi PR.

## 2. The 9 pain points — TUI vs architectural

| # | Pain | Category | Fix lives in |
|---|---|---|---|
| 1 | Sync state drift | **Architectural** | State model (§4) |
| 2 | No mental model | **UX** | TUI redesign (§5) — status header |
| 3 | Misleading labels | **UX** | TUI redesign (§5) — intent-based actions |
| 4 | Phases destroy each other's work | **Architectural** | Phase reorg (§6) — cleanup opt-in |
| 5 | No "what's on the server" view | **Architectural** + UX | Server endpoint + Inspect screen |
| 6 | No "force re-sync this chat" path | **UX** | Sync screen — recovery action |
| 7 | Multiple overlapping entry points | **Architectural** | Phase reorg (§6) — collapse `explore_backup.py extract` |
| 8 | Useless 401 from Mikoshi | **UX** + small architectural | `push_via_api.py` error decoder |
| 9 | Cron vs interactive divergence | **Architectural** | Share `_best_from_phase` between paths |

**Found-during-review, not on the original list:**

- **A. Stale lock file footgun.** `acquire_lock()` (run_pipeline.sh:342) writes `$$` to `.pipeline.lock` but never checks if the PID is alive. A `kill -9` during a 6-hour cron interval bricks every subsequent cron run until manual cleanup. Fix: check `kill -0 $(cat lock)` and break stale locks; log loudly.
- **B. `temp/` vs `MIKOSHI_BACKUP_DIR` asymmetry.** `find_existing_chatstorage()` checks both paths but `--from-phase` requires `MIKOSHI_BACKUP_DIR` to be set (run_pipeline.sh:717). A user without an external SSD has the data on disk but can't use the fast path. Either remove the dual-path support or make `--from-phase` work for both.
- **C. Schema version is hard-coded on both ends (`"1.2"`).** Bumping requires coordinated deploy. Trivial server-side `negotiate` step (client offers `[1.2, 1.3]`, server picks highest understood) would decouple deploys. Low priority but flagged for the next schema bump.
- **D. Lock file vs locked iPhone race.** If the iPhone trust prompt hangs Phase 1 indefinitely, the lock file persists. Add a Phase-1 timeout (e.g. 5 min) before pairing fails loudly.
- **E. `KEEP_LOCAL_EXPORTS=5` with 419 MB exports = 2+ GB local.** Not a bug but worth surfacing in `status` (current size of `exports/`).

## 3. User journeys

For each row: **Want** = one sentence; **Current** = steps the user takes today; **Pain** = where they get confused / what goes wrong; **Server outcome** = observable end state on Mikoshi.

| Journey | Want | Current path | Pain points today | Server-side outcome |
|---|---|---|---|---|
| **First-run setup** | Get a fresh install pushing to Mikoshi for the first time. | `setup.sh` → `verify_setup.sh` → edit `~/.mikoshi-ingest.conf` → `security add-generic-password` → `mikoshi-whatsapp.sh` → ⚙ Verify setup → 🌍 Sync — full. | Token comes from a Mikoshi UI the user has to navigate to; no in-TUI link. No dry-run before pushing 96 GB. No way to test auth alone. | Account scoped row in `ingestion_event` + per-message rows in `messages` for everything iPhone holds. |
| **Daily incremental (iPhone plugged)** | Pull today's messages and push. | Open TUI → 🔁 Sync — incremental → ⚡ Extract-only or 🔄 Refresh from iPhone (unclear which). | Two equally-plausible options, no guidance on cost. Status doesn't say "iPhone is reachable, so Refresh is the right choice." | Delta of messages since last commit appended; cursor advances. |
| **Catch-up after a week away** | iPhone is plugged again, want to fetch a week's backlog. | Same as daily; backup takes longer; user can't tell if it's stuck or just slow. | No ETA from `idevicebackup2`. `backup_progress.py` gives progress for Phase 2 but Phase 3+4 have no progress bar for the user. | Same as daily, just more messages. |
| **Push one contact's full history** | Backfill Alice from the beginning. | 👤 Sync — one contact only → pick from list → ⚡ Extract-only → enter → wait. | If the contact's local cursor is already advanced (from a prior partial push), this still skips messages older than the cursor. No "force-from-zero for this chat" option. | New messages for that JID appended; *gaps* below the cursor remain invisible until manually rewound. |
| **Force re-sync (server lost data)** | Server table is empty/wrong; push everything again. | Delete `.sync_state.json` → 🌍 Sync — full. | Loses cursors for *other* chats too. No surgical rewind. Server dedups on `external_id` so the bytes uploaded are mostly wasted re-uploads of attachments the server already has. | All messages re-submitted; server dedups; only missing rows get inserted. |
| **Re-key (new iPhone)** | iPhone replaced — new UDID, new Keychain password possibly. | `reset-backup --force` → re-pair → re-run sync. | UDID change invalidates the local backup tree; `--from-phase` then breaks because UDID dir doesn't exist. Error not actionable. | Continues appending; same Mikoshi account; cursor unchanged. |
| **Token rotation** | Mikoshi regenerated the token. | Edit `~/.mikoshi-ingest.conf` → re-run. | If you forget, you 401 silently and *cursors advance* (this is pain point #1). No "test auth" action. | Failed push → no server change → local lies until next push. |
| **Cron sync, no iPhone** | Mac woke up at 3am but iPhone isn't on the same network. | `mikoshi-whatsapp.sh sync` runs Phase 1, fails immediately, exits non-zero. | Whole cron run is "FAIL" in logs; no fallback to "decrypt+extract what we have" or "skip cleanly." | Nothing happens server-side. |
| **Recover from bad cursor / wrong push** | Pushed wrong data; want to start over for one chat. | `sqlite3 .sync_state.json` (can't — it's JSON) → manually edit JSON → re-run. | No CLI for rewinding a single chat's cursor. No undo on the server side. | Server retains the bad data; client re-pushes; dedup keeps the bad data. |
| **Inspect what's already on the server** | "Did the last sync actually land?" | `ssh jetson; sqlite3 mikoshi.db 'SELECT MAX(timestamp) FROM messages WHERE …'`. | Not discoverable. Requires SSH + SQL knowledge. | N/A — read-only. |

## 4. New state model

### 4.1 Principle

> **The server owns the cursor. The client mirrors it.**

The client's `.sync_state.json` becomes a *cache* of the server's
view, refreshed at known checkpoints. Local extraction is bounded by
the *cached* server cursor, never by a local-only watermark.

Failure modes that the new model makes impossible:

| Old failure | New behavior |
|---|---|
| Push 401s → local cursor advances → data on server is missing | Cursor never advances on the client at all unless the commit succeeded; if it did, the value comes *from* the server response. |
| User deletes `.sync_state.json` and re-runs → "full sync" against a populated server → 96 GB re-upload | First action of any sync is `GET /cursors` → cache repopulates from server → only the actual delta is extracted. |
| Two chats sync OK, third fails partway → all three cursors are written together | Per-chat cursor only advances if that chat's messages appear in the commit response's `committed_cursors`. |

### 4.2 Cursor flow (sequence)

```
┌─────────┐         ┌────────────┐         ┌──────────────┐
│  TUI    │         │  Pipeline  │         │  Mikoshi     │
└────┬────┘         └─────┬──────┘         └──────┬───────┘
     │  "Sync now"        │                       │
     │ ──────────────────►│                       │
     │                    │ GET /cursors          │
     │                    │ ─────────────────────►│
     │                    │ ◄─────────────────────│ {jid: ts}
     │                    │ reconcile with DB     │
     │                    │ → plan (N msgs)       │
     │ ◄──────────────────│ "Plan: 1,247 msgs"    │
     │  "go"              │                       │
     │ ──────────────────►│                       │
     │                    │ extract → manifest    │
     │                    │ POST /manifest        │
     │                    │ ─────────────────────►│
     │                    │ ◄─────────────────────│ push_id, needs_media[]
     │                    │ upload media          │
     │                    │ POST /commit          │
     │                    │ ─────────────────────►│
     │                    │ ◄─────────────────────│ committed_cursors{jid: ts}
     │                    │ write cache.json      │
     │                    │  (only committed)     │
     │ ◄──────────────────│ "1,247 in, 3 chats"   │
```

If `commit` fails or never reaches success, **no write to local cache
happens**. The next run repeats the same plan (idempotent thanks to
`external_id` dedup).

### 4.3 File on disk (new shape)

```json
{
  "version": 2,
  "server_url": "https://jetson:7777",
  "last_cursor_refresh": "2026-05-26T18:42:00Z",
  "last_successful_commit": "2026-05-26T18:42:13Z",
  "last_push_id": "01HXY...",
  "chats": {
    "120363406808051406@g.us": {
      "committed_through_ts": "2026-05-26T07:14:17Z",
      "committed_through_external_id": "ios:1834219",
      "source": "server"
    }
  }
}
```

- `committed_through_external_id` is the strong guarantee (the actual
  last message the server has); `committed_through_ts` is for human
  display and fallback.
- `source: "server"` flags entries set by the server's commit response;
  if we ever have to seed from local extraction (offline server, see
  §4.6) we mark `"source": "extracted (offline)"` so reconciliation
  knows to verify.

### 4.4 Drift detection

On every TUI open and every sync start, the client compares:

```
local cache ts  vs  server cursor ts  vs  DB latest message ts
       │                    │                       │
       └──── if differs ────┴──── this is "drift" ──┘
```

Three drift cases:

| Case | Symptom | Action |
|---|---|---|
| **Local ahead of server** (the original bug) | Server `ts=X`, local cache `ts=Y > X`. | Trust server. Surface `⚠ Drift: 4 chats — re-syncing will recover` in header. Next extraction uses server's `X` as the cutoff. |
| **DB ahead of both** | Newer messages on iPhone. | Normal case — the gap is what gets pushed. |
| **Server ahead of local cache** | Cache stale; another machine pushed (won't happen for single-user but possible). | Refresh cache silently from server. |

### 4.5 Server-side changes (separate PR on Mikoshi repo)

- `GET /api/ingest/v1/cursors` → returns `{<account>: {<jid>: {ts, external_id}}}`. Already trivially derivable from the existing `messages` table or the `ingestion_cursor` table — add an index if needed.
- `POST /api/ingest/v1/commit` response gains `committed_cursors: {<jid>: {ts, external_id}}`. The server already computes the max-per-chat to advance its own cursor; just echo it back.
- (Optional) `POST /api/ingest/v1/cursors/rewind` for the recovery flow — `{jid, ts}` rewinds the server cursor (server-side messages stay; dedup will skip on next push). Gated by admin token.

These two endpoints are small (<100 LOC server-side). Flag for separate PR.

### 4.6 Fallback when server endpoint isn't available

For deploy-staging, keep current behavior under a flag:

```
MIKOSHI_TRUST_LOCAL_CURSOR=1    # legacy mode, prints a warning
```

Default unset → require the new endpoint and fail early if `GET /cursors` returns 404 with a clear "Mikoshi server needs upgrade" message. This avoids silently regressing to the old footgun.

## 5. New TUI

### 5.1 Top-level menu (5 actions)

```
┌─── Mikoshi WhatsApp ──────────────────────────────────────────┐
│ iPhone:   ✓ detected (00008130-..., last seen 2 min ago)      │
│ Backup:   /Volumes/SSD/iphone_backup  (96.4 GB, 1 device)     │
│ Decrypt:  ✓ ChatStorage fresh (extracted 18:42)               │
│ Server:   ✓ jetson:7777   3 chats tracked   last commit 09:13 │
│ State:    ⚠ Drift: 4 chats local-ahead — re-sync will recover │
└────────────────────────────────────────────────────────────────┘

  ▶ Sync                  (default — opens sync screen)
    Inspect
    Favorites             3 chats marked favorite
    Setup & verify
    Tools (advanced)
    Exit
```

When `State` row is green (no drift), `Sync` is highlighted. When drift
is present, the header explicitly says how to recover and `Sync` does
the right thing automatically.

### 5.2 Status header — fields and sources

| Field | Source | Color rules |
|---|---|---|
| iPhone | `idevice_id -l` — cached 30s | green=detected, yellow=`MIKOSHI_BACKUP_DIR` reachable so OK to proceed without, red=neither |
| Backup | `MIKOSHI_BACKUP_DIR` + `du -sk` (timeout 5s) | green=present + non-empty, red=path missing |
| Decrypt | `extracted/ChatStorage.sqlite` + SQLite magic header | green=valid, yellow=stale (>24h), red=missing/corrupt |
| Server | `GET /cursors` with 3s timeout | green=200, yellow=200 but cursors empty, red=4xx/5xx/timeout |
| State | drift computed (§4.4) | green=in-sync, yellow=local ahead (drift), red=local behind server (impossible single-user, but flagged) |

### 5.3 Sync screen (replaces 4 of today's 14 actions)

After opening Sync the user sees the **plan** before any work happens:

```
┌─── Sync plan ─────────────────────────────────────────────────┐
│ Source:  iPhone (incremental backup ~ 2 min)                   │
│ Scope:   3 favorite chats                                      │
│ Server cursors:                                                │
│   "Alice"    last_committed 2026-05-25 17:33                  │
│   "Family"   last_committed 2026-05-26 07:14                  │
│   "Mom"      last_committed 2026-05-26 07:58                  │
│ Estimated new:  ~1,200 messages, ~40 attachments               │
│                                                                │
│ Drift recovery: 4 chats had local-ahead cursors — re-sync will │
│ rewind to server cursor and replay (~3,800 messages dedup).    │
└────────────────────────────────────────────────────────────────┘

  ▶ Sync now
    Change scope (all chats / one chat / favorites)
    Change source (iPhone refresh / use cached backup)
    Skip media (text-only for this run)
    Dry run (extract but don't push)
    Cancel
```

This is the heart of the redesign. **Nothing happens until the user sees
what's about to happen.** "0 messages" goes from being a footgun to
being the *expected and shown-in-advance* outcome.

### 5.4 Inspect screen

```
  Local chats (from ChatStorage)        128
  Server messages (per /cursors)         3 chats tracked
  Drift summary:
    ✓ Alice          local==server
    ⚠ Family         local 18:42 > server 09:13 (drift)
    ✓ Mom            local==server
    — Bob            no server record (never pushed)

  ▶ Show all chats (table)
    Show drift only
    Open ChatStorage in sqlite3 shell
    Back
```

### 5.5 Setup & verify

Combines today's `Verify setup`, `Verify backup integrity`, `Edit config`,
plus a new `Test Mikoshi connection` (does `GET /cursors` and decodes
the response).

### 5.6 Tools (advanced)

`Push existing export`, `Run tests`, `Toggle preserve extracted`,
`Rewind cursor for one chat` (recovery action — server PR needed),
`Reset backup`.

### 5.7 TUI state machine

```
                        (open TUI)
                            │
                            ▼
                    ┌─── HEADER ───┐
                    │  refresh all │
                    │  signals (parallel,
                    │  with timeouts)
                    └──────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
         iPhone        Backup         Server
        (idevice)     (du, plist)    (GET /cursors)
            │              │              │
            └──────────────┼──────────────┘
                           ▼
                    ┌──────────────┐
                    │  compute     │
                    │  drift +     │
                    │  recommend   │
                    │  primary     │
                    │  action      │
                    └──────┬───────┘
                           ▼
                       MAIN MENU
```

All signals refresh **on every menu return** so the header is always
current.

## 6. Phase reorganization

### 6.1 New phase shape

```
Old:  detect → backup → decrypt(all) → extract → validate
      → cleanup → push → gc                       (7 phases)

New:  prepare → acquire → decrypt-db → plan
      → materialize → push&confirm → gc           (6 phases, 1 conditional)
```

| New phase | Replaces | Why the change |
|---|---|---|
| **1. prepare** | `detect_device` | Same job, but doesn't fail when no iPhone if `acquire` can fall back to cached backup. Sets `SOURCE=iphone\|cache`. |
| **2. acquire** | `create_backup` | When `SOURCE=cache`, this is a no-op pre-flight check. Otherwise = today's Phase 2. |
| **3. decrypt-db** | First half of `decrypt_backup` | Decrypts only ChatStorage.sqlite (~10s, not 13 min). Fast enough to do unconditionally on every run. Enables `plan` to query the DB. |
| **4. plan** | *new* | Reads server cursors, queries DB for messages beyond cursor, computes work-to-do. Surfaces to TUI as the plan screen. Decides whether media-decrypt is needed and which subset. |
| **5. materialize** | Second half of `decrypt_backup` + `extract_and_export` + `validate_export` | Decrypts the media subset planned in step 4, extracts messages, writes manifest, validates schema. Single phase because they share the same scope. |
| **6. push&confirm** | `sync_remote` + cursor write | Atomic. Pushes manifest → media → commit. Cursor write happens **only inside this phase** and only from the server's `committed_cursors` response. |
| **7. gc** (opt) | `gc_local_exports` | Unchanged. Still gated on `SYNC_SUCCEEDED`. |
| **(removed)** | `secure_cleanup` | Demoted to a separate one-shot command (`mikoshi-whatsapp.sh purge-extracted`). Default config has been "preserve" since `caaa140` — keeping a phase that's mostly a no-op is mental-model noise. |

### 6.2 Answers to the user's specific questions

- **Should extract and push be merged?** Yes — that's Phase 4+5 (plan/materialize) feeding Phase 6, with cursor write *inside* Phase 6. The merge is logical, not literal: separate scripts, but cursor side-effects gated behind commit success.
- **Should decrypt be split into DB-only and media-only?** Yes. Phase 3 = DB only, Phase 5 materialize handles media. The current "decrypt everything then maybe extract one chat" path is wasteful when planning could narrow scope first.
- **Should secure_cleanup be opt-in?** Yes. It is *de facto* opt-in already (preserve is default since `caaa140`); making it explicit means removing the dead phase from the default flow. Keep the `shred` capability as `mikoshi-whatsapp.sh purge-extracted` for the security-conscious case.
- **Where do retries / partial failures live?** Phase 6 owns retries: the existing per-media exponential backoff (push_via_api.py:108-133) stays; manifest POST and commit POST get the same treatment. *No retry crosses a phase boundary* — that's how cursors stayed consistent before, and we preserve it.

### 6.3 Cursor write location (the one rule)

```
def commit_with_cursor_write(state_file, push_id, ...):
    status, body = post_json(f"{url}/commit", token, {"push_id": push_id})
    if status != 200:
        raise CommitFailed(status, body)         # NO cursor write
    write_cursor_cache(state_file, body["committed_cursors"])
    return body
```

There is exactly **one** place in the codebase that writes
`.sync_state.json` after this redesign: a function called from Phase 6
after `commit` returns 200. All other writes are removed.

## 7. Migration plan

Staged so a current install never breaks mid-migration. Order matters.

### Stage M1 — non-breaking groundwork (1 PR, low risk)

- Add a `purge-extracted` subcommand to `mikoshi-whatsapp.sh` (one-shot opt-in shred — replaces today's reliance on Phase 5 doing it).
- Add `Test Mikoshi connection` action in TUI (just hits `GET /accounts/me` or any auth-only endpoint to validate `MIKOSHI_TOKEN`).
- Add 401-decoder in `push_via_api.py` (parse server response body for known phrases; map to actionable text).
- Add stale-lock detection in `acquire_lock()` (`kill -0` check).
- Fix `temp/` vs `MIKOSHI_BACKUP_DIR` asymmetry in `--from-phase` (run_pipeline.sh:716).
- Add Phase-1 pairing timeout (5 min default; `MIKOSHI_DEVICE_TIMEOUT=300`).

Backwards compat: none broken. New behavior only.

### Stage M2 — server endpoint, client uses optionally (1 server PR + 1 client PR, medium risk)

- **Server PR**: add `GET /api/ingest/v1/cursors`. Echo `committed_cursors` in commit response.
- **Client PR**: `push_via_api.py` reads `committed_cursors` from commit response. If present → write a `.sync_state.json.v2` alongside the existing file. *No behavior change for the existing file yet.*

A user with old Mikoshi keeps working. A user with new Mikoshi starts accumulating the v2 file passively.

### Stage M3 — flip cursor authority (1 client PR, high risk — but reversible)

- `extract_messages.py` stops writing `.sync_state.json` (rename existing writes to a deprecation warning).
- Phase 6 becomes the sole writer (using the v2 schema written passively in M2).
- TUI status reads v2 if present, falls back to v1 with a "legacy cursor file" warning.
- Add `MIKOSHI_TRUST_LOCAL_CURSOR=1` escape hatch that re-enables the old extraction-side write for one release cycle.

### Stage M4 — TUI redesign (1 client PR, medium risk)

- New menu structure, status header, plan-then-act flow.
- Old action functions (`action_full_backup`, `action_backup_one_contact`, etc.) become internal helpers driven by the new screens. Don't delete — keep as test surface.

### Stage M5 — phase reorganization (1 client PR, medium risk)

- Split `decrypt_backup` into `decrypt_db` + lazy media decrypt.
- Add `plan` phase between decrypt-db and extract.
- Remove `secure_cleanup` from default flow (kept as `purge-extracted` subcommand from M1).

### Stage M6 — cron parity (1 client PR, low risk)

- `mikoshi-whatsapp.sh sync` imports the same `_best_from_phase` (move to a shared Python module `pipeline_state.py`) and picks the right starting phase.
- Cron with no iPhone + cached backup → falls back to extract-only sync. Logs "iPhone unreachable, using cached backup from $TS".
- Cron with no iPhone + no backup → exits clean (rc=0) with "nothing to do" instead of "failed."

### Stage M7 — cleanup (1 client PR, low risk)

- Delete `explore_backup.py extract` subcommand (overlap eliminated; `mikoshi-whatsapp.sh sync --from-phase 4` covers the use case).
- Delete unused legacy `CONFIG_FILE` rsync handling in `run_pipeline.sh:17`.
- Remove `MIKOSHI_TRUST_LOCAL_CURSOR` escape hatch.

### Backwards-compatible env vars

All existing env vars keep working. New ones, none of them required:

| New env var | Default | Purpose |
|---|---|---|
| `MIKOSHI_TRUST_LOCAL_CURSOR` | unset (=0) | M3 escape hatch — re-enables old extraction-time cursor write |
| `MIKOSHI_DEVICE_TIMEOUT` | 300 | Phase 1 pairing timeout (seconds) |
| `MIKOSHI_DRY_RUN` | unset | Compute plan, write export, skip push entirely |

## 8. Files to change — ranked by impact

Risk: L=low (mechanical), M=medium (logic shift, tests catch regressions), H=high (state semantics).

| File | New / Change | LOC delta | Risk | Stage |
|---|---|---|---|---|
| `extract_messages.py` | Remove cursor write (`save_sync_state` call); just return the *would-be* cursors. | −20 / +5 | **H** | M3 |
| `push_via_api.py` | Add cursor-cache write on commit success; add 401 decoder; surface `committed_cursors`. | +60 / −5 | M | M2+M3 |
| `tui.py` | New menu, header, sync-plan screen, inspect screen. | +400 / −300 | M | M4 |
| `run_pipeline.sh` | Split decrypt phase; add plan phase; remove `secure_cleanup` from main flow. | +60 / −80 | M | M5 |
| `mikoshi-whatsapp.sh` | Add `purge-extracted` subcommand; share `_best_from_phase` for `sync` path. | +50 / −10 | L | M1+M6 |
| **`pipeline_state.py`** (new) | Shared state helpers: load/save cursor cache, drift detection, `_best_from_phase`. | +200 | M | M2 |
| `selective_decrypt.py` | Add `decrypt_media_only(relpaths)` helper for the plan-driven path. | +30 | L | M5 |
| `explore_backup.py` | Delete `extract` subcommand; keep `list-chats` and `shell`. | −50 | L | M7 |
| `schema.json` | Add `1.3` to enum if Stage M2 needs new fields (probably not — cursors live in HTTP response only). | +0 / +5 | L | M2 |
| `verify_setup.sh` | Add server-auth check (curl `GET /cursors`). | +20 | L | M1 |
| `README.md` | Document new TUI, new cursor model, `purge-extracted`. | +60 / −20 | L | M4+M7 |
| `tests/` (existing) | New tests: cursor never advances on push failure; drift detection; plan computation. | +150 | L | every stage |

**Total estimated delta:** ~+1,000 / −500 lines across 12 files over 7 PRs.

## 9. Open questions

Surface these — these are non-obvious decisions and the user should pick:

1. **Server cursor granularity: JID or `(account, JID, external_id)`?** Proposed per-JID + external_id. An alternative is to record the highest committed `external_id` only and *compute* "what's missing" purely from set-difference at push time. That's robust but pushes more data through the manifest endpoint. Trade-off depends on Mikoshi's tolerance for large manifests.
2. **Rewind UX — manual or automatic on drift?** When drift is detected, should the client *automatically* rewind to server cursor and re-sync (silent), or always require the user to click "Reconcile drift"? Auto = safer; manual = more transparent.
3. **Favorites moving server-side?** Today `~/.mikoshi-favorites.json` is local-only. If you sync from two Macs (you don't, but…) they could diverge. Probably not worth moving — flag and skip unless it bites.
4. **`MIKOSHI_TRUST_LOCAL_CURSOR` lifetime.** Proposed scope: "one release cycle." Is that the right window for a single-user tool, or just delete the legacy path on M3 since you control both client and server?
5. **`--full` semantics.** Today, `--full` resets local state only. Should it also call a (new) `POST /api/ingest/v1/account/<id>/purge` on the server? Today's behavior + dedup means `--full` is mostly a *cosmetic* full sync (the bytes go through, server discards dupes). If the goal is *actually* wiping the server view too, that needs a separate flow.
6. **Plan caching.** The plan is cheap (one SQL query + one HTTP call) but on a 96 GB external SSD even cheap is "a few seconds." Cache the plan for N seconds? Probably no — the freshness is the value.
7. **Schema negotiation.** Item C in §2 — worth bundling with M2 or punt to a future schema-bump PR?
8. **TUI status header refresh on every menu return** — could feel sluggish if `GET /cursors` takes 800ms over WiFi. Throttle to "refresh if older than 30s," with manual refresh action? I lean yes.

---

## Verification plan (for the eventual implementation PRs)

For each stage:

- **M1** — Manual: run `mikoshi-whatsapp.sh purge-extracted` and confirm `extracted/` is gone, `backup/<UDID>/` untouched. Trigger 401 by hand and confirm decoder fires. Kill `idevicebackup2 backup` mid-run, then re-run `sync` — should detect stale lock and proceed.
- **M2** — Run a sync against the new server. Inspect `.sync_state.json.v2`. Compare against `.sync_state.json` — they should match. Server endpoint test via `curl -H "Authorization: Bearer $T" $URL/api/ingest/v1/cursors`.
- **M3** — Critical: simulate the original bug. Disable the token on the server. Run `sync`. Confirm `.sync_state.json` does **not** advance. Re-enable token, re-run. Confirm pushes recover correctly.
- **M4** — TUI smoke test: open, observe header populates within 3s, click Sync, see plan, cancel before confirm, return to menu. No subprocess spawned.
- **M5** — Time `--from-phase 3` vs `--from-phase 4` on a single-chat selective sync. Expect ~10s vs ~5s (DB-only decrypt vs cached).
- **M6** — Cron simulation: unplug iPhone, run `mikoshi-whatsapp.sh sync` → expect rc=0 and a "no iPhone, using cached backup" message in `cron_<ts>.log`.
- **M7** — Static check: nothing references `explore_backup.py extract`. Pytest passes.

End-to-end smoke after M3 lands (the high-risk stage):

1. Add 5 new messages on iPhone.
2. Run TUI → Sync → confirm plan shows "≥ 5 new."
3. Confirm Sync. Wait. See "5 messages, 0 attachments" → ok.
4. Verify on server: `SELECT COUNT(*) FROM messages WHERE account=? AND timestamp > ?` shows +5.
5. Run TUI → Sync again → plan shows "0 new" → correctly does nothing.
6. Disable token. Run sync. Expect error (decoded), no cache change.
7. Re-enable. Run sync. Expect "0 new" again (no drift). Cache matches server.
