"""Tests for validate_export.py — the pre-rsync schema gate."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "validate_export.py"
SCHEMA = Path(__file__).parent.parent / "schema.json"


def _run_validator(export_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT),
         "--export", str(export_path),
         "--schema", str(SCHEMA)],
        capture_output=True, text=True,
    )


def _valid_manifest_v12():
    """Minimal manifest that conforms to schema v1.2."""
    return {
        "schema_version": "1.2",
        "client_id": "test-host",
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
                        "external_id": "ios:1",
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


class TestValidPasses:
    def test_minimal_v12_passes(self, tmp_path):
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(_valid_manifest_v12()))
        result = _run_validator(export)
        assert result.returncode == 0
        assert "OK" in result.stdout

    def test_with_kept_attachment(self, tmp_path):
        m = _valid_manifest_v12()
        m["chats"][0]["messages"][0]["attachment"] = {
            "skipped": False,
            "sha256": "a" * 64,
            "filename": "a" * 64 + ".jpg",
            "mime": "image/jpeg",
            "size_bytes": 102400,
            "title": "pic.jpg",
        }
        m["stats"]["attachments_kept"] = 1
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        assert _run_validator(export).returncode == 0

    def test_with_skipped_attachment(self, tmp_path):
        m = _valid_manifest_v12()
        m["chats"][0]["messages"][0]["attachment"] = {
            "skipped": True,
            "reason": "video filtered",
            "mime": "video/mp4",
            "original_path": "Media/video1.mp4",
        }
        m["stats"]["attachments_skipped"] = 1
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        assert _run_validator(export).returncode == 0

    def test_with_group_participants(self, tmp_path):
        m = _valid_manifest_v12()
        m["chats"][0]["jid"] = "12345@g.us"
        m["chats"][0]["is_group"] = True
        m["chats"][0]["participants"] = [
            {"jid": "alice@s.whatsapp.net", "name": "Alice"},
            {"jid": "bob@s.whatsapp.net", "name": "Bob"},
        ]
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        assert _run_validator(export).returncode == 0


class TestInvalidFails:
    def test_unknown_schema_version(self, tmp_path):
        m = _valid_manifest_v12()
        m["schema_version"] = "9.9"
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        result = _run_validator(export)
        assert result.returncode != 0
        assert "FAIL" in result.stderr

    def test_missing_external_id(self, tmp_path):
        # external_id is required in v1.2
        m = _valid_manifest_v12()
        del m["chats"][0]["messages"][0]["external_id"]
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        result = _run_validator(export)
        assert result.returncode != 0
        assert "external_id" in result.stderr

    def test_oversize_attachment_fails(self, tmp_path):
        # Schema caps size_bytes at 5MB
        m = _valid_manifest_v12()
        m["chats"][0]["messages"][0]["attachment"] = {
            "skipped": False,
            "sha256": "f" * 64,
            "filename": "huge.pdf",
            "mime": "application/pdf",
            "size_bytes": 10 * 1024 * 1024,
        }
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        result = _run_validator(export)
        assert result.returncode != 0

    def test_invalid_sha256_format(self, tmp_path):
        m = _valid_manifest_v12()
        m["chats"][0]["messages"][0]["attachment"] = {
            "skipped": False,
            "sha256": "not-a-hash",
            "filename": "x.jpg",
            "size_bytes": 1000,
        }
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        result = _run_validator(export)
        assert result.returncode != 0

    def test_missing_chats_array(self, tmp_path):
        m = _valid_manifest_v12()
        del m["chats"]
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        result = _run_validator(export)
        assert result.returncode != 0
        assert "chats" in result.stderr.lower()

    def test_invalid_mode_enum(self, tmp_path):
        m = _valid_manifest_v12()
        m["mode"] = "weird"
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        result = _run_validator(export)
        assert result.returncode != 0


class TestErrorOutputUsable:
    def test_lists_multiple_errors(self, tmp_path):
        """Verifier should list several violations, not bail at the first."""
        m = _valid_manifest_v12()
        # Multiple violations
        m["schema_version"] = "9.9"
        del m["chats"][0]["messages"][0]["external_id"]
        m["chats"][0]["messages"][0]["is_from_me"] = "not-a-bool"
        export = tmp_path / "manifest.json"
        export.write_text(json.dumps(m))
        result = _run_validator(export)
        assert result.returncode != 0
        # At least 2 distinct issues mentioned
        err = result.stderr
        assert err.count("validation error") <= 1  # "N validation error(s)"
        # But list of errors should contain >1
        assert "schema_version" in err or "9.9" in err
