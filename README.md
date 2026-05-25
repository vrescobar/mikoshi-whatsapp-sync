# mikoshi-whatsapp-sync

WhatsApp → Mikoshi data pipeline. Two components:

| Component | Where it runs | What it does |
|---|---|---|
| **[whatsapp_export/](whatsapp_export/)** | macOS (your Mac, alongside the iPhone) | Backs up the iPhone, decrypts WhatsApp's `ChatStorage.sqlite`, exports a schema-validated JSON + filtered attachments, rsyncs to the Mikoshi server. |
| **[server/](server/)** | Mikoshi server (Linux) | Watches the rsync target dir, validates each JSON, upserts into PostgreSQL, stores attachments in a bucketed media tree. |

The two halves communicate via [`whatsapp_export/schema.json`](whatsapp_export/schema.json) — the only contract. Bump its `schema_version` to coordinate breaking changes.

## Quick start

```bash
# macOS side
cd whatsapp_export
bash setup.sh
bash verify_setup.sh
# (store backup password in Keychain, edit ~/.whatsapp_export.conf)
bash run_pipeline.sh

# Linux side (on Mikoshi server)
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
psql $DATABASE_URL -f migrations/001_init.sql
cp .env.example /etc/mikoshi/ingestor.env  # edit
python -m mikoshi_ingestor.cli ingest --once   # or --watch
```

Detailed docs:
- macOS pipeline: [whatsapp_export/README.md](whatsapp_export/README.md), [whatsapp_export/QUICKSTART.md](whatsapp_export/QUICKSTART.md)
- Mikoshi ingestor: [server/README.md](server/README.md)
- Plan / roadmap: `~/.claude/plans/whatsapp-export-pipeline.md`

## Tests

```bash
source whatsapp_export/.venv/bin/activate
python -m pytest -v
```

30 tests cover: timestamp conversion, attachment filters (no video, ≤5MB), sha256 dedup, incremental/full/full-contact sync, group participants, system-message filtering, schema conformance, validation, attachment storage idempotency.
