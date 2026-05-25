"""Unit tests for the ingestor — focuses on validation and attachment storage.

Tests requiring a real PostgreSQL are not included here; they belong in an
integration test suite gated by a CI service container.
"""

import json
import sys
from pathlib import Path

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from mikoshi_ingestor.ingestor import store_attachment, validate


SCHEMA_PATH = Path(__file__).parent.parent.parent / "whatsapp_export" / "schema.json"


def _valid_export(tmp_path):
    """Build a minimal export JSON that conforms to schema.json v1.1."""
    return {
        "schema_version": "1.1",
        "exported_at": "2026-05-25T14:00:00+00:00",
        "mode": "full",
        "target_contact": None,
        "include_system_messages": False,
        "chats": [
            {
                "jid": "alice@s.whatsapp.net",
                "name": "Alice",
                "is_group": False,
                "participants": [],
                "messages": [
                    {
                        "id": 1,
                        "timestamp": "2026-05-25T13:00:00+00:00",
                        "from_jid": "alice@s.whatsapp.net",
                        "to_jid": "me@s.whatsapp.net",
                        "is_from_me": False,
                        "push_name": "Alice",
                        "text": "hola",
                        "type": 0,
                        "attachment": None,
                    },
                ],
            },
        ],
        "stats": {
            "total_chats": 1,
            "total_messages": 1,
            "system_messages_skipped": 0,
            "attachments_kept": 0,
            "attachments_skipped": 0,
        },
    }


class TestValidate:
    def test_minimal_valid_export(self, tmp_path):
        export = tmp_path / "ex.json"
        export.write_text(json.dumps(_valid_export(tmp_path)))
        data = validate(export, SCHEMA_PATH)
        assert data["schema_version"] == "1.1"

    def test_unknown_schema_version_rejected(self, tmp_path):
        bad = _valid_export(tmp_path)
        bad["schema_version"] = "9.9"
        export = tmp_path / "ex.json"
        export.write_text(json.dumps(bad))
        with pytest.raises(jsonschema.ValidationError):
            validate(export, SCHEMA_PATH)

    def test_missing_required_field_rejected(self, tmp_path):
        bad = _valid_export(tmp_path)
        del bad["chats"]
        export = tmp_path / "ex.json"
        export.write_text(json.dumps(bad))
        with pytest.raises(jsonschema.ValidationError):
            validate(export, SCHEMA_PATH)

    def test_oversized_attachment_rejected(self, tmp_path):
        bad = _valid_export(tmp_path)
        bad["chats"][0]["messages"][0]["attachment"] = {
            "skipped": False,
            "sha256": "a" * 64,
            "filename": "a" * 64 + ".jpg",
            "mime": "image/jpeg",
            "size_bytes": 10 * 1024 * 1024,  # 10MB > 5MB max in schema
        }
        export = tmp_path / "ex.json"
        export.write_text(json.dumps(bad))
        with pytest.raises(jsonschema.ValidationError):
            validate(export, SCHEMA_PATH)


class TestStoreAttachment:
    def test_moves_file_to_bucket(self, tmp_path):
        src_dir = tmp_path / "inbox"
        src_dir.mkdir()
        store = tmp_path / "store"

        sha = "abcdef1234" + "0" * 54  # 64 chars
        filename = f"{sha}.jpg"
        (src_dir / filename).write_bytes(b"hello")

        dest = store_attachment(sha, src_dir, store, filename)

        assert dest.exists()
        assert dest.parent.name == "ab"  # bucketed by first 2 chars
        assert not (src_dir / filename).exists()

    def test_idempotent_second_call(self, tmp_path):
        src_dir = tmp_path / "inbox"
        src_dir.mkdir()
        store = tmp_path / "store"

        sha = "ff" + "0" * 62
        filename = f"{sha}.jpg"
        (src_dir / filename).write_bytes(b"hello")
        store_attachment(sha, src_dir, store, filename)

        # Same file appears again in inbox (a later export references it)
        (src_dir / filename).write_bytes(b"hello")
        dest = store_attachment(sha, src_dir, store, filename)

        assert dest.exists()
        # Inbox copy was cleaned up
        assert not (src_dir / filename).exists()

    def test_missing_source_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            store_attachment("a" * 64, tmp_path, tmp_path / "store", "nope.jpg")
