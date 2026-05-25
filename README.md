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

## Quick start

```bash
cd whatsapp_export
bash setup.sh
bash verify_setup.sh
# Configure: ~/.mikoshi-ingest.conf
#   MIKOSHI_URL=https://your-mikoshi.example.com
#   MIKOSHI_TOKEN=<paste from /accounts/<id>/ingestion in Mikoshi>
bash run_pipeline.sh                                  # incremental sync
bash run_pipeline.sh --mode full-contact --contact "Alice"   # one chat
bash run_pipeline.sh --mode full                      # everything
```

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
