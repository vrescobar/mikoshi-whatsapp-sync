# mikoshi-whatsapp-sync

WhatsApp → Mikoshi push pipeline. Runs on a macOS host, extracts an iPhone
WhatsApp backup, and pushes the result to a Mikoshi server's REST ingest API.

The server side lives **inside Mikoshi** itself (`src/ingestion/`, exposed at
`/api/ingest/v1/*`). There is no standalone server component in this repo.

| Stage | What it does |
|---|---|
| `setup.sh` | Installs `libimobiledevice`, Python deps. |
| `run_pipeline.sh` | Orchestrates: device backup → decrypt → extract → push. |
| `extract_messages.py` | Reads `ChatStorage.sqlite`, writes a v1.2 manifest + sha256-keyed attachments. |
| `push_via_api.py` | Submits the manifest → uploads missing media → commits. |
| `mikoshi-whatsapp.sh` | Single entrypoint: opens TUI by default, or `sync` for cron. |
| `tui.py` | Interactive menu — manage favorites, sync, push, inspect. |

## Quick start

```bash
cd whatsapp_export
bash setup.sh
bash verify_setup.sh
# Configure: ~/.mikoshi-ingest.conf
#   MIKOSHI_URL=https://your-mikoshi.example.com
#   MIKOSHI_TOKEN=<paste from /accounts/<id>/ingestion in Mikoshi>

./mikoshi-whatsapp.sh                  # interactive TUI (default)
./mikoshi-whatsapp.sh sync             # cron-friendly: favorites if set, else all
./mikoshi-whatsapp.sh sync --all       # force all chats incrementally
./mikoshi-whatsapp.sh status           # show config & state
```

## Selective sync (single chat)

When you only care about one DM or group, pass `--chat-jid` to short-circuit
two expensive steps:

- **Phase 3 (decryption)** only decrypts `ChatStorage.sqlite` plus the media
  attachments that belong to that chat (queried from `ZWAMEDIAITEM`),
  instead of the whole WhatsApp shared domain. Cuts disk usage and
  runtime by 1-2 orders of magnitude for a single-chat dump.
- **Phase 4 (extraction)** filters on `ZCONTACTJID = ?` (exact match — no
  substring surprises like `--contact`).

```bash
# Decrypt + extract only this one chat, since 2026-01-01, no remote push:
./mikoshi-whatsapp.sh sync \
    --chat-jid '34xxxxxxxxx@s.whatsapp.net' \
    --since 2026-01-01 \
    --skip-remote-sync
```

`--since` combines with the per-chat incremental cursor: it lifts the
lower bound for fresh chats, but never rewinds a chat whose cursor is
already past that date.

### Iterating without re-decrypting

By default the pipeline now **keeps the decrypted artifacts** under
`MIKOSHI_BACKUP_DIR/extracted/` between runs (defaults to *preserve*),
so the next invocation can pass `--from-phase 4` and skip the ~30 min
of decryption. Persist the flag in `~/.mikoshi-ingest.conf`:

```
# Default — keep ChatStorage.sqlite + media decrypted across runs.
# Flip to false if you want extracted/ wiped after every successful run.
MIKOSHI_PRESERVE_EXTRACTED=true
```

TUI exposes this as a one-key toggle:

```
mikoshi-whatsapp.sh   →   🔐  Toggle keep decrypted between runs
```

Accepted values (case-insensitive): `true/yes/on/1` vs `false/no/off/0`.
The encrypted iPhone backup under `backup/<UDID>/` is always preserved —
only the decrypted subtree is affected by this flag.

The TUI's "Backup one contact" picker now propagates the chosen JID as
`--chat-jid` automatically when you select from the chat list (selective
decrypt kicks in for free). Free-form typing still uses `--contact`
(substring match) unless what you typed looks like a JID.

## Favorites + cron

You can mark a subset of chats as "favorites" — those are the only ones
synced when you run `sync` from cron. Manage them from the TUI:

```
mikoshi-whatsapp.sh   →   📌 Manage favorites   →   Add chats
```

Favorites are stored in `~/.mikoshi-favorites.json` (overridable via
`MIKOSHI_FAVORITES_FILE`). Match is by JID, so renaming a contact is fine.

Example cron — sync favorites every 6h, log to a file:

```cron
0 */6 * * * /Users/you/projects/mikoshi-whatsapp-sync/whatsapp_export/mikoshi-whatsapp.sh sync >> ~/mikoshi-cron.log 2>&1
```

The lock file (`.pipeline.lock`) prevents concurrent runs if a backup is
still in progress when the next interval fires. If no favorites are
configured, `sync` falls back to an incremental run over all chats.

The Mikoshi server is the **only opinionated piece**: it validates the
manifest, dedupes media by content hash, persists messages with
account-scoped attribution, and queues them for the configured AI scan
(transcription, vision, observer memory). Mikoshi-side config lives at
`/accounts/<id>/ingestion/edit` (token, filters, AI overrides, cron).

Re-pushing the same export is safe — the server is idempotent on
`(account_id, external_id)` per message and on `content_hash` per media file.

## Schema

[`whatsapp_export/schema.json`](whatsapp_export/schema.json) — JSON Schema
1.2. Bumped from 1.1 to add `external_id` (per-message stable id derived
from `ZWAMESSAGE.Z_PK`) and `client_id` (sending hostname). Earlier
versions are rejected by the Mikoshi REST API.

## Tests

```bash
source whatsapp_export/.venv/bin/activate
python -m pytest -v
```
