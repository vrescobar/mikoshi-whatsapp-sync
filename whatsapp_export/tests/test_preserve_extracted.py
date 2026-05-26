"""
Tests for the MIKOSHI_PRESERVE_EXTRACTED-related helpers in tui.py.

Post-redesign the env var no longer drives the cleanup behaviour at all
(the cleanup trap is now a no-op by default; shred is opt-in via
MIKOSHI_SECURE_CLEANUP=1). The parse_bool / set_conf_value helpers stay
because the TUI's "Toggle keep decrypted between runs" action still
flips this value as a user-facing preference, and the var is still listed
in INGEST_CONF_KEYS so existing configs don't lose their setting.

The bash-side behaviour is now covered by test_cleanup_safety.py.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

PIPELINE = Path(__file__).parent.parent / "run_pipeline.sh"


# ─── Python helpers in tui.py ─────────────────────────────────────────────


@pytest.fixture
def tui_module():
    sys.modules.pop("tui", None)
    import tui
    return tui


class TestParseBool:
    def test_truthy_forms(self, tui_module):
        for v in ("true", "True", "TRUE", "yes", "YES", "on", "1"):
            assert tui_module.parse_bool(v, default=False) is True, v

    def test_falsy_forms(self, tui_module):
        for v in ("false", "False", "no", "NO", "off", "0"):
            assert tui_module.parse_bool(v, default=True) is False, v

    def test_whitespace_is_tolerated(self, tui_module):
        assert tui_module.parse_bool("  true  ", default=False) is True
        assert tui_module.parse_bool("\tfalse\n", default=True) is False

    def test_none_returns_default(self, tui_module):
        assert tui_module.parse_bool(None, default=True) is True
        assert tui_module.parse_bool(None, default=False) is False

    def test_empty_returns_default(self, tui_module):
        assert tui_module.parse_bool("", default=True) is True
        assert tui_module.parse_bool("   ", default=False) is False

    def test_garbage_returns_default(self, tui_module):
        assert tui_module.parse_bool("maybe", default=False) is False
        assert tui_module.parse_bool("maybe", default=True) is True


class TestSetConfValue:
    def _read(self, p: Path) -> dict:
        out = {}
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def test_creates_file_if_missing(self, tui_module, tmp_path):
        path = tmp_path / "fresh.conf"
        tui_module.set_conf_value("FOO", "bar", conf_path=path)
        assert path.exists()
        assert self._read(path)["FOO"] == "bar"

    def test_appends_when_key_absent(self, tui_module, tmp_path):
        path = tmp_path / "x.conf"
        path.write_text("EXISTING=old\n")
        tui_module.set_conf_value("FOO", "bar", conf_path=path)
        kv = self._read(path)
        assert kv == {"EXISTING": "old", "FOO": "bar"}

    def test_replaces_in_place_when_key_present(self, tui_module, tmp_path):
        path = tmp_path / "x.conf"
        path.write_text(
            "# header\n"
            "A=1\n"
            "MIKOSHI_PRESERVE_EXTRACTED=true\n"
            "B=2\n"
        )
        tui_module.set_conf_value(
            "MIKOSHI_PRESERVE_EXTRACTED", "false", conf_path=path,
        )
        lines = path.read_text().splitlines()
        assert lines[0] == "# header"
        assert lines[1] == "A=1"
        assert lines[2] == "MIKOSHI_PRESERVE_EXTRACTED=false"
        assert lines[3] == "B=2"

    def test_preserves_comments_and_blank_lines(self, tui_module, tmp_path):
        path = tmp_path / "x.conf"
        original = (
            "# top comment\n"
            "\n"
            "# section\n"
            "A=1\n"
            "\n"
            "# trailing\n"
        )
        path.write_text(original)
        tui_module.set_conf_value("NEW", "v", conf_path=path)
        result = path.read_text()
        for orig_line in original.splitlines():
            assert orig_line in result.splitlines()
        assert "NEW=v" in result.splitlines()

    def test_atomic_write_no_tmp_residue(self, tui_module, tmp_path):
        path = tmp_path / "x.conf"
        path.write_text("A=1\n")
        tui_module.set_conf_value("B", "2", conf_path=path)
        assert not (tmp_path / "x.conf.tmp").exists()

    def test_exports_to_environment(self, tui_module, tmp_path, monkeypatch):
        path = tmp_path / "x.conf"
        monkeypatch.delenv("SOME_KEY", raising=False)
        tui_module.set_conf_value("SOME_KEY", "bar", conf_path=path)
        assert os.environ.get("SOME_KEY") == "bar"


class TestLoadIngestConfPicksUpPreserve:
    def test_preserve_is_in_known_keys(self, tui_module):
        # Regression guard: existing user configs reference this key. Keep
        # it in INGEST_CONF_KEYS even though the new pipeline doesn't act
        # on it — losing it would silently break the UI toggle.
        assert "MIKOSHI_PRESERVE_EXTRACTED" in tui_module.INGEST_CONF_KEYS

    def test_file_value_exported_to_env(self, tui_module, tmp_path, monkeypatch):
        conf = tmp_path / "ingest.conf"
        conf.write_text("MIKOSHI_PRESERVE_EXTRACTED=false\n")
        monkeypatch.setattr(tui_module, "INGEST_CONF", conf)
        monkeypatch.delenv("MIKOSHI_PRESERVE_EXTRACTED", raising=False)

        cfg = tui_module.load_ingest_conf()
        assert cfg["MIKOSHI_PRESERVE_EXTRACTED"] == "false"
        assert os.environ["MIKOSHI_PRESERVE_EXTRACTED"] == "false"


# ─── --help still mentions the cleanup-relevant env vars ──────────────────


class TestHelp:
    def test_help_advertises_secure_cleanup(self):
        """The new opt-in shred env var must be discoverable."""
        result = subprocess.run(
            ["bash", str(PIPELINE), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "MIKOSHI_SECURE_CLEANUP" in result.stdout

    def test_help_advertises_trust_local_cursor(self):
        """The cursor-write escape hatch must be discoverable."""
        result = subprocess.run(
            ["bash", str(PIPELINE), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "MIKOSHI_TRUST_LOCAL_CURSOR" in result.stdout
