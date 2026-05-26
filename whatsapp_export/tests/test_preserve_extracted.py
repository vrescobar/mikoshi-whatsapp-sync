"""
Tests for the MIKOSHI_PRESERVE_EXTRACTED feature:

  - parse_bool / set_conf_value helpers in tui.py
  - cleanup() in run_pipeline.sh honours the env var on success
  - default behaviour is "preserve" (decrypted artifacts kept across runs)

The shell test reuses the cleanup-extraction trick from
test_cleanup_safety.py: pull cleanup() out of run_pipeline.sh, source it
in an ad-hoc bash runner, run it against a synthetic tree.
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
    # Re-import each test so monkeypatched INGEST_CONF doesn't leak.
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
        # Guard the user's typo doesn't silently flip behaviour either way.
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
        # Same line index, comment + neighbours intact, value flipped.
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
        # Original lines unchanged; new line appended at the end.
        result = path.read_text()
        for orig_line in original.splitlines():
            assert orig_line in result.splitlines()
        assert "NEW=v" in result.splitlines()

    def test_atomic_write_no_tmp_residue(self, tui_module, tmp_path):
        path = tmp_path / "x.conf"
        path.write_text("A=1\n")
        tui_module.set_conf_value("B", "2", conf_path=path)
        # No half-written tmp left behind
        assert not (tmp_path / "x.conf.tmp").exists()

    def test_exports_to_environment(self, tui_module, tmp_path, monkeypatch):
        path = tmp_path / "x.conf"
        monkeypatch.delenv("SOME_KEY", raising=False)
        tui_module.set_conf_value("SOME_KEY", "bar", conf_path=path)
        # New value should be visible to subsequent reads / subprocesses
        assert os.environ.get("SOME_KEY") == "bar"


class TestLoadIngestConfPicksUpPreserve:
    def test_preserve_is_in_known_keys(self, tui_module):
        # Regression guard: if someone removes MIKOSHI_PRESERVE_EXTRACTED
        # from INGEST_CONF_KEYS the file value silently stops propagating
        # to run_pipeline.sh, which would re-enable the original bug.
        assert "MIKOSHI_PRESERVE_EXTRACTED" in tui_module.INGEST_CONF_KEYS

    def test_file_value_exported_to_env(self, tui_module, tmp_path, monkeypatch):
        conf = tmp_path / "ingest.conf"
        conf.write_text("MIKOSHI_PRESERVE_EXTRACTED=false\n")
        monkeypatch.setattr(tui_module, "INGEST_CONF", conf)
        monkeypatch.delenv("MIKOSHI_PRESERVE_EXTRACTED", raising=False)

        cfg = tui_module.load_ingest_conf()
        assert cfg["MIKOSHI_PRESERVE_EXTRACTED"] == "false"
        assert os.environ["MIKOSHI_PRESERVE_EXTRACTED"] == "false"


# ─── Shell cleanup() honours the env var ──────────────────────────────────


def _extract_cleanup_fn() -> str:
    """Pull the cleanup() function body out of run_pipeline.sh."""
    script_text = PIPELINE.read_text()
    start = script_text.find("cleanup() {")
    depth = 0
    end = start
    for i, ch in enumerate(script_text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return script_text[start:end]


def _run_cleanup(temp_dir: Path, *, env_preserve: str | None,
                 exit_code: int = 0) -> subprocess.CompletedProcess:
    """
    Source cleanup() with a synthetic environment and run it against
    `temp_dir`. `env_preserve` is the literal value of
    MIKOSHI_PRESERVE_EXTRACTED to inject (None = explicitly unset, so the
    cleanup sees the "default" branch instead of inheriting whatever the
    pytest worker has in os.environ).
    """
    cleanup_fn = _extract_cleanup_fn()
    runner = f"""
set +e
log()   {{ echo "LOG: $@"; }}
error() {{ echo "ERR: $@" >&2; }}
warn()  {{ echo "WARN: $@"; }}
info()  {{ echo "INFO: $@"; }}
TEMP_DIR="{temp_dir}"
TEMP_DIR_IS_EXTERNAL=true
LOCK_FILE="/tmp/test-preserve-lock-$$"
{cleanup_fn}

# Force cleanup to see exit_code={exit_code} via $? at entry.
(exit {exit_code}); trap - EXIT; cleanup
"""
    # Build a minimal env. Crucially we DON'T inherit
    # MIKOSHI_PRESERVE_EXTRACTED from os.environ — earlier tests in this
    # suite may have exported it via load_ingest_conf() and we want each
    # test to see its own value (or genuine absence) in isolation.
    env = {k: v for k, v in os.environ.items()
           if k != "MIKOSHI_PRESERVE_EXTRACTED"}
    if env_preserve is not None:
        env["MIKOSHI_PRESERVE_EXTRACTED"] = env_preserve
    return subprocess.run(
        ["bash", "-c", runner], env=env,
        capture_output=True, text=True, timeout=30,
    )


def _seed_extracted(tmp_path: Path):
    """Make extracted/ look like a real decrypted tree."""
    extracted = tmp_path / "extracted"
    extracted.mkdir(parents=True)
    (extracted / "ChatStorage.sqlite").write_bytes(b"SQLite format 3\x00" + b"x" * 4096)
    (extracted / "media").mkdir()
    (extracted / "media" / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 1024)
    return extracted


class TestCleanupPreservesOnDefault:
    """Default (env var unset) must preserve extracted/ on success."""

    def test_default_preserves_on_success(self, tmp_path):
        extracted = _seed_extracted(tmp_path)
        result = _run_cleanup(tmp_path, env_preserve=None, exit_code=0)
        assert result.returncode == 0, result.stderr
        # extracted/ must still be there — the whole point of the new default.
        assert extracted.exists(), \
            "Default behaviour should be to KEEP extracted/ on success"
        assert (extracted / "ChatStorage.sqlite").exists()
        assert "Keeping extracted/" in result.stdout

    def test_explicit_true_preserves_on_success(self, tmp_path):
        extracted = _seed_extracted(tmp_path)
        result = _run_cleanup(tmp_path, env_preserve="true", exit_code=0)
        assert result.returncode == 0
        assert extracted.exists()

    def test_accepts_yes_on_1(self, tmp_path):
        for val in ("yes", "ON", "1", "TrUe"):
            extracted = _seed_extracted(tmp_path / val)
            result = _run_cleanup(tmp_path / val, env_preserve=val, exit_code=0)
            assert result.returncode == 0, f"{val} → {result.stderr}"
            assert extracted.exists(), f"{val} should preserve but didn't"


class TestCleanupOptsOut:
    """MIKOSHI_PRESERVE_EXTRACTED=false must wipe extracted/ on success."""

    def test_explicit_false_wipes_on_success(self, tmp_path):
        extracted = _seed_extracted(tmp_path)
        result = _run_cleanup(tmp_path, env_preserve="false", exit_code=0)
        assert result.returncode == 0
        # The whole extracted/ subtree should be gone.
        assert not extracted.exists(), \
            "MIKOSHI_PRESERVE_EXTRACTED=false should remove extracted/"

    def test_no_off_0_all_wipe(self, tmp_path):
        for val in ("no", "OFF", "0", "False"):
            extracted = _seed_extracted(tmp_path / val)
            result = _run_cleanup(tmp_path / val, env_preserve=val, exit_code=0)
            assert result.returncode == 0
            assert not extracted.exists(), f"{val} should wipe but didn't"


class TestCleanupPreservesOnFailureRegardless:
    """On pipeline failure, extracted/ stays unless explicitly told otherwise.
    Lets the user --from-phase 4 to retry the broken step.
    """

    def test_failure_preserves_even_when_unset(self, tmp_path):
        extracted = _seed_extracted(tmp_path)
        result = _run_cleanup(tmp_path, env_preserve=None, exit_code=1)
        assert result.returncode == 0  # cleanup itself succeeds
        assert extracted.exists(), "Failure-path must preserve for retry"

    def test_failure_with_explicit_off_still_wipes(self, tmp_path):
        # Power user explicitly said "wipe even on failure". Respect it.
        extracted = _seed_extracted(tmp_path)
        result = _run_cleanup(tmp_path, env_preserve="false", exit_code=2)
        assert result.returncode == 0
        assert not extracted.exists()


class TestHelp:
    def test_help_advertises_preserve_env(self):
        result = subprocess.run(
            ["bash", str(PIPELINE), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert "MIKOSHI_PRESERVE_EXTRACTED" in result.stdout
        # The new default — true — must be discoverable from --help.
        assert "DEFAULT" in result.stdout.upper() and \
               "true" in result.stdout.lower()
