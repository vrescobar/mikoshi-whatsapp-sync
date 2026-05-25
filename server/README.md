# Mikoshi Ingestor

Server-side component that receives WhatsApp export JSONs from the macOS pipeline and ingests them into PostgreSQL for retrieval / training.

## Architecture

```
~/whatsapp_exports/                ← rsync target on Mikoshi server
├── whatsapp_export_*.json         ← schema-validated exports
└── attachments/
    └── <sha256>.<ext>             ← content-addressed media

   │
   │ (cron: every 5 min)
   ▼
mikoshi_ingestor (Python CLI)
   │
   ├─ validate JSON against schema.json
   ├─ upsert chats, messages, participants
   ├─ move attachments → /var/lib/mikoshi/media/
   └─ index message.text → pgvector (optional)
```

## Database schema

```sql
CREATE TABLE chats (
    jid          TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    is_group     BOOLEAN NOT NULL DEFAULT false,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE participants (
    chat_jid     TEXT REFERENCES chats(jid) ON DELETE CASCADE,
    member_jid   TEXT,
    name         TEXT,
    PRIMARY KEY (chat_jid, member_jid)
);

CREATE TABLE messages (
    chat_jid     TEXT REFERENCES chats(jid) ON DELETE CASCADE,
    id           BIGINT NOT NULL,
    ts           TIMESTAMPTZ,
    from_jid     TEXT,
    to_jid       TEXT,
    is_from_me   BOOLEAN NOT NULL,
    push_name    TEXT,
    text         TEXT,
    msg_type     INT,
    attachment_sha256 TEXT REFERENCES attachments(sha256),
    PRIMARY KEY (chat_jid, id)
);

CREATE INDEX messages_ts_idx ON messages (ts);
CREATE INDEX messages_chat_ts_idx ON messages (chat_jid, ts);

CREATE TABLE attachments (
    sha256       TEXT PRIMARY KEY,
    filename     TEXT NOT NULL,
    mime         TEXT,
    size_bytes   BIGINT NOT NULL,
    storage_path TEXT NOT NULL,    -- where the file lives on disk
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ingestion_log (
    id           BIGSERIAL PRIMARY KEY,
    export_file  TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    chats_seen   INT,
    messages_upserted INT,
    attachments_stored INT,
    status       TEXT,             -- 'ok' | 'fail'
    error        TEXT
);
```

## Install (on Mikoshi server)

```bash
cd /opt/mikoshi
git clone <repo> .
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt

# Apply schema
psql $DATABASE_URL -f server/migrations/001_init.sql
```

## Configure

```bash
cp server/.env.example /etc/mikoshi/ingestor.env
# Edit DATABASE_URL, EXPORTS_DIR, MEDIA_STORE
```

## Run

```bash
# One-shot ingest of all pending exports
python -m mikoshi_ingestor.cli ingest --once

# Watch mode (loops every 5 min)
python -m mikoshi_ingestor.cli ingest --watch

# Ingest a single file (debugging)
python -m mikoshi_ingestor.cli ingest --file /path/to/whatsapp_export_*.json

# Show stats
python -m mikoshi_ingestor.cli stats
```

Recommended: run as systemd unit (`server/systemd/mikoshi-ingestor.service`).

## Idempotency

- Messages are upserted by `(chat_jid, id)`. Re-running a full sync over an
  existing DB doesn't duplicate anything.
- Attachments are inserted with `ON CONFLICT (sha256) DO NOTHING` and the
  file is only moved if not already present in `MEDIA_STORE`.
- After successful ingest, the JSON is moved to `EXPORTS_DIR/processed/`
  with a timestamp so the directory doesn't grow forever.

## Failure handling

Failed ingests are logged to `ingestion_log` with status='fail'. The JSON
stays in `EXPORTS_DIR/` and will be retried on the next run. Files that
fail validation are moved to `EXPORTS_DIR/quarantine/`.
