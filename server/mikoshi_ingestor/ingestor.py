"""Core ingestion logic — validate, upsert, move attachments."""

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jsonschema
import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    export_file: Path
    chats_seen: int = 0
    messages_upserted: int = 0
    attachments_stored: int = 0
    status: str = "pending"
    error: Optional[str] = None


def validate(export_path: Path, schema_path: Path) -> dict:
    """Load and validate the export. Raises on failure."""
    schema = json.loads(schema_path.read_text())
    data = json.loads(export_path.read_text())
    jsonschema.Draft7Validator(schema).validate(data)
    return data


def store_attachment(
    sha256: str,
    src_dir: Path,
    media_store: Path,
    filename: str,
) -> Path:
    """
    Move attachment from src_dir/filename to media_store, named by sha256.
    Returns final path. Idempotent: if dest exists with same sha256, skip.
    """
    src = src_dir / filename
    if not src.exists():
        raise FileNotFoundError(f"Attachment not found at expected path: {src}")

    # Bucket by first 2 chars to avoid massive flat dirs
    bucket = media_store / sha256[:2]
    bucket.mkdir(parents=True, exist_ok=True)

    ext = Path(filename).suffix
    dest = bucket / f"{sha256}{ext}"

    if not dest.exists():
        shutil.move(str(src), str(dest))
    else:
        # Attachment already stored — remove the duplicate from the inbox
        try:
            src.unlink()
        except FileNotFoundError:
            pass

    return dest


def ingest_export(
    export_path: Path,
    schema_path: Path,
    media_store: Path,
    conn: psycopg.Connection,
) -> IngestResult:
    """Validate, then upsert into PostgreSQL inside a single transaction."""
    result = IngestResult(export_file=export_path)

    try:
        data = validate(export_path, schema_path)
    except (jsonschema.ValidationError, json.JSONDecodeError) as e:
        result.status = "fail"
        result.error = f"validation: {e}"
        return result

    attachments_dir = export_path.parent / "attachments"

    try:
        with conn.transaction():
            log_id = _log_start(conn, str(export_path))

            for chat in data.get("chats", []):
                _upsert_chat(conn, chat)
                result.chats_seen += 1

                for participant in chat.get("participants", []):
                    _upsert_participant(conn, chat["jid"], participant)

                for msg in chat.get("messages", []):
                    att_sha = None
                    att = msg.get("attachment")
                    if att and not att.get("skipped"):
                        # Store the file first so the FK is satisfied
                        try:
                            dest = store_attachment(
                                att["sha256"], attachments_dir, media_store, att["filename"]
                            )
                            _upsert_attachment(conn, att, dest)
                            att_sha = att["sha256"]
                            result.attachments_stored += 1
                        except FileNotFoundError:
                            # The JSON references a file that isn't here. Keep
                            # the message but drop the attachment link.
                            logger.warning(
                                "Attachment missing for chat=%s msg=%s sha=%s",
                                chat["jid"], msg["id"], att.get("sha256"),
                            )

                    _upsert_message(conn, chat["jid"], msg, att_sha)
                    result.messages_upserted += 1

            result.status = "ok"
            _log_finish(conn, log_id, result)
    except Exception as e:
        result.status = "fail"
        result.error = repr(e)
        logger.exception("Ingest failed for %s", export_path)

    return result


# ─── SQL helpers ───────────────────────────────────────────────────────────

def _upsert_chat(conn, chat):
    conn.execute(
        """
        INSERT INTO chats (jid, name, is_group, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (jid) DO UPDATE
          SET name = EXCLUDED.name,
              is_group = EXCLUDED.is_group,
              updated_at = now()
        """,
        (chat["jid"], chat["name"], chat.get("is_group", False)),
    )


def _upsert_participant(conn, chat_jid, p):
    conn.execute(
        """
        INSERT INTO participants (chat_jid, member_jid, name)
        VALUES (%s, %s, %s)
        ON CONFLICT (chat_jid, member_jid) DO UPDATE
          SET name = EXCLUDED.name
        """,
        (chat_jid, p["jid"], p.get("name")),
    )


def _upsert_attachment(conn, att, storage_path: Path):
    conn.execute(
        """
        INSERT INTO attachments (sha256, filename, mime, size_bytes, storage_path)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (sha256) DO NOTHING
        """,
        (
            att["sha256"],
            att["filename"],
            att.get("mime"),
            att["size_bytes"],
            str(storage_path),
        ),
    )


def _upsert_message(conn, chat_jid, msg, attachment_sha256):
    conn.execute(
        """
        INSERT INTO messages
            (chat_jid, id, ts, from_jid, to_jid, is_from_me,
             push_name, text, msg_type, attachment_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (chat_jid, id) DO UPDATE SET
            ts = EXCLUDED.ts,
            from_jid = EXCLUDED.from_jid,
            to_jid = EXCLUDED.to_jid,
            is_from_me = EXCLUDED.is_from_me,
            push_name = EXCLUDED.push_name,
            text = EXCLUDED.text,
            msg_type = EXCLUDED.msg_type,
            attachment_sha256 = EXCLUDED.attachment_sha256
        """,
        (
            chat_jid,
            msg["id"],
            msg.get("timestamp"),
            msg.get("from_jid"),
            msg.get("to_jid"),
            msg["is_from_me"],
            msg.get("push_name"),
            msg.get("text"),
            msg.get("type"),
            attachment_sha256,
        ),
    )


def _log_start(conn, export_file: str) -> int:
    row = conn.execute(
        """
        INSERT INTO ingestion_log (export_file, status)
        VALUES (%s, 'running')
        RETURNING id
        """,
        (export_file,),
    ).fetchone()
    return row[0]


def _log_finish(conn, log_id: int, result: IngestResult):
    conn.execute(
        """
        UPDATE ingestion_log
           SET finished_at = now(),
               chats_seen = %s,
               messages_upserted = %s,
               attachments_stored = %s,
               status = %s,
               error = %s
         WHERE id = %s
        """,
        (
            result.chats_seen,
            result.messages_upserted,
            result.attachments_stored,
            result.status,
            result.error,
            log_id,
        ),
    )
