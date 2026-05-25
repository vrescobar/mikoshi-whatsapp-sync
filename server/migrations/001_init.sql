-- Mikoshi ingestor — initial schema.
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS attachments (
    sha256        TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    mime          TEXT,
    size_bytes    BIGINT NOT NULL,
    storage_path  TEXT NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chats (
    jid           TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    is_group      BOOLEAN NOT NULL DEFAULT false,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS participants (
    chat_jid      TEXT NOT NULL REFERENCES chats(jid) ON DELETE CASCADE,
    member_jid    TEXT NOT NULL,
    name          TEXT,
    PRIMARY KEY (chat_jid, member_jid)
);

CREATE TABLE IF NOT EXISTS messages (
    chat_jid           TEXT NOT NULL REFERENCES chats(jid) ON DELETE CASCADE,
    id                 BIGINT NOT NULL,
    ts                 TIMESTAMPTZ,
    from_jid           TEXT,
    to_jid             TEXT,
    is_from_me         BOOLEAN NOT NULL,
    push_name          TEXT,
    text               TEXT,
    msg_type           INT,
    attachment_sha256  TEXT REFERENCES attachments(sha256),
    PRIMARY KEY (chat_jid, id)
);

CREATE INDEX IF NOT EXISTS messages_ts_idx ON messages (ts);
CREATE INDEX IF NOT EXISTS messages_chat_ts_idx ON messages (chat_jid, ts DESC);
CREATE INDEX IF NOT EXISTS messages_from_idx ON messages (from_jid);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id                  BIGSERIAL PRIMARY KEY,
    export_file         TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    chats_seen          INT,
    messages_upserted   INT,
    attachments_stored  INT,
    status              TEXT,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS ingestion_log_finished_idx
    ON ingestion_log (finished_at DESC);

COMMIT;
