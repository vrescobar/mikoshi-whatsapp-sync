# mikoshi-whatsapp-sync

WhatsApp → Mikoshi push pipeline. Runs on a macOS host, extracts an iPhone
WhatsApp backup, and pushes the result to a Mikoshi server's REST ingest API.

The server side lives **inside Mikoshi** itself (`src/ingestion/`, exposed at
`/api/ingest/v1/*`). There is no standalone server component in this repo.

> **Recent redesign**: cursor advancement is now controlled exclusively by
> the server's response to `/commit`. See [REDESIGN.md](REDESIGN.md) for the
> full reasoning, and [MIKOSHI_SERVER_PATCH.md](MIKOSHI_SERVER_PATCH.md) for
> the small server-side endpoints the new client expects (the client
> degrades gracefully when they're absent).

| Stage | What it does |
|---|---|
| `setup.sh` | Installs `libimobiledevice`, Python deps. |
| `run_pipeline.sh` | Orchestrates: device backup → decrypt → extract → push. |
| `extract_messages.py` | Reads `ChatStorage.sqlite`, writes a v1.2 manifest + sha256-keyed attachments. **Never writes the cursor cache anymore** (unless `MIKOSHI_TRUST_LOCAL_CURSOR=1`). |
| `push_via_api.py` | Submits the manifest → uploads missing media → commits → **writes the cursor cache from the server's commit response**. |
| `pipeline_state.py` | Shared state helpers: cursor cache, drift detection, plan computation, `best_from_phase`. Used by the TUI and the cron path so they agree. |
| `mikoshi-whatsapp.sh` | Single entrypoint: TUI by default, `sync`/`status`/`test-auth`/`purge-extracted`/`reset-backup`/`verify-backup`. |
| `tui.py` | Interactive menu — Sync, Inspect (drift), Favorites, Setup & verify, Tools. |

## Quick start

```bash
cd whatsapp_export
bash setup.sh
bash verify_setup.sh
# Configure ~/.mikoshi-ingest.conf:
#   MIKOSHI_URL=https://your-mikoshi.example.com
#   MIKOSHI_TOKEN=<paste from /accounts/<id>/ingestion in Mikoshi>

./mikoshi-whatsapp.sh test-auth        # validate token without pushing anything
./mikoshi-whatsapp.sh                  # interactive TUI
./mikoshi-whatsapp.sh sync             # cron-friendly: favorites if set, else all
./mikoshi-whatsapp.sh status           # config + state header
```

## How sync works now

The pipeline is intent-driven — open the TUI and pick **Sync**. The screen
shows a **plan before doing anything**:

```
┌─── Sync plan ─────────────────────────────────────────────────┐
│ Source:  cached decrypted DB (extract → push only)             │
│ Scope:   favorites                                             │
│ New:     1,247 messages across 3/3 chats, 40 attachments       │
└────────────────────────────────────────────────────────────────┘
  ▶ Sync now
    Cancel
```

The plan is computed by querying ChatStorage.sqlite locally and comparing
against the server's per-chat cursors (`GET /api/ingest/v1/cursors`).
"0 messages" means "the server already has everything past your local
cursors" — it's the success path, not a bug.

### Cursors live server-side

The pre-redesign pipeline advanced `.sync_state.json` immediately after
extraction. A push that 401'd left local cursors lying — the next run
reported "0 messages" with no warning while the server had nothing.

After the redesign:

- `push_via_api.py` is the **only** thing that writes `.sync_state.json`,
  and only after `/commit` returns 200.
- `extract_messages.py` reads the cache but never writes it (unless
  `MIKOSHI_TRUST_LOCAL_CURSOR=1`, the legacy escape hatch).
- Failed push = no cursor movement, anywhere. Re-running the sync replays
  the same plan; the server dedups on `external_id`.
- Deleting `.sync_state.json` is safe — first action of any sync re-fetches
  cursors from the server.

### Drift detection

The TUI status header surfaces drift before you click anything:

```
iPhone:   ✓ detected (00008130-..., last seen 2 min ago)
Backup:   /Volumes/SSD/iphone_backup  (96.4 GB, 1 device)
Decrypt:  ✓ ChatStorage fresh
Server:   ✓ jetson:7777   3 chats tracked   last commit 09:13
State:    ⚠ Drift: 4 chats local-ahead — re-sync will recover
```

`Inspect` (top-level menu) shows the per-chat drift table.

## Updating after a few days away

Plug the iPhone. Open `./mikoshi-whatsapp.sh`. Pick **🔂 Sync**. Choose
scope (favorites/all/one) and source (iPhone refresh / cached backup).
All four phases are incremental:

- **Phase 2** (`idevicebackup2`) reuses the existing backup directory and
  only fetches files whose hash changed since the last backup.
- **Phase 3a** (`selective_decrypt.decrypt_db_only`) decrypts
  ChatStorage.sqlite only (~10s).
- **Phase 3b** decrypts only the media files belonging to the planned
  scope (one chat / favorites / all).
- **Phase 4** (`extract_messages.py`) reads the per-chat cursor from the
  cache and only emits messages newer than the cursor.
- **Phase 5** (`push_via_api.py`) sends the manifest first; the server
  responds with `needs_media[]` so only attachments the server lacks are
  uploaded. After `/commit` returns 200 the cursor cache is updated from
  the server's `committed_cursors` echo.

## Selective sync (single chat)

```bash
./mikoshi-whatsapp.sh sync \
    --chat-jid '34xxxxxxxxx@s.whatsapp.net' \
    --since 2026-01-01 \
    --skip-remote-sync
```

`--chat-jid` switches Phase 3b to per-chat selective decryption (only
ChatStorage.sqlite plus that chat's media is decrypted) and Phase 4 to
exact-JID filtering.

`--since` lifts the lower bound for fresh chats; never rewinds a chat
whose cursor is already past that date.

## Iterating without re-decrypting

By default the pipeline keeps the decrypted artifacts under
`MIKOSHI_BACKUP_DIR/extracted/` between runs so `--from-phase 4` can skip
the ~30 min decrypt. Toggle in `~/.mikoshi-ingest.conf`:

```
MIKOSHI_PRESERVE_EXTRACTED=true
```

The TUI's **Setup & verify** screen exposes this as a one-key toggle.

To shred decrypted artifacts on demand (replaces the pre-redesign
`Phase 5 secure_cleanup` that used to run on every successful sync):

```bash
./mikoshi-whatsapp.sh purge-extracted [--force]
```

Or set `MIKOSHI_SECURE_CLEANUP=1` for that env var to trigger the shred at
the end of every successful pipeline run.

## Favorites + cron

Mark a subset of chats as "favorites" so cron sync only touches them:

```bash
./mikoshi-whatsapp.sh   →   📌 Manage favorites   →   Add chats
```

Stored at `~/.mikoshi-favorites.json` (override with
`MIKOSHI_FAVORITES_FILE`). Match is by JID, so renaming is fine.

Example cron — sync favorites every 6h, log to a file:

```cron
0 */6 * * * /Users/you/projects/mikoshi-whatsapp-sync/whatsapp_export/mikoshi-whatsapp.sh sync >> ~/mikoshi-cron.log 2>&1
```

When the iPhone isn't reachable but a cached backup exists, the cron
path falls back to extract-only (the TUI's smart phase detection is
now shared with the cron path via `pipeline_state.best_from_phase`).
When neither is available the run exits with rc=0 and "nothing to do"
rather than failing.

The lock file (`.pipeline.lock`) prevents concurrent runs. Stale locks
are detected and reclaimed automatically (the pre-redesign behaviour
required manual `rm` after a `kill -9`).

## Schema

[`whatsapp_export/schema.json`](whatsapp_export/schema.json) — JSON Schema
1.2. `external_id` (per-message stable id derived from `ZWAMESSAGE.Z_PK`)
and `client_id` (sending hostname). Earlier versions are rejected by the
Mikoshi REST API.

## Server-side patches needed

The new model uses two endpoints not present in pre-redesign Mikoshi:

- `GET /api/ingest/v1/cursors` — returns per-chat watermarks.
- `POST /api/ingest/v1/commit` echoes `committed_cursors`.

The client degrades gracefully when these are missing — see
[MIKOSHI_SERVER_PATCH.md](MIKOSHI_SERVER_PATCH.md) for shapes and
implementation hints.

## Tests

```bash
source whatsapp_export/.venv/bin/activate
python -m pytest -v
```
