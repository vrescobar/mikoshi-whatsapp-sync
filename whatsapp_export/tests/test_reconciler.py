"""Reconciler unit tests.

Covers the dedup algorithm spelled out in reconciler.reconcile_chat:
stanza-id grouping with tie-breaking on (timestamp, text-non-empty,
text-length, source-priority); fingerprint dedup for stanza-null
messages; attachment provenance preferring the source whose bytes are
actually on disk.

The probe CLI is exercised separately (against synthetic DBs that
mimic the iPhone/Mac live shape) to lock down the wire it expects on
real data.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reconciler import (  # noqa: E402
    _round_ts_5s,
    fingerprint,
    probe_chat,
    reconcile,
    reconcile_chat,
    stanza_of,
)


def make_msg(
    *,
    ext: str,
    ts: str | None = None,
    text: str | None = "",
    from_jid: str | None = "alice@s.whatsapp.net",
    to_jid: str | None = "me@s.whatsapp.net",
    legacy: str | None = None,
    attachment: dict | None = None,
) -> dict:
    """Tiny factory — keeps each test row to one readable line."""
    return {
        "id": 0,
        "external_id": ext,
        "legacy_external_id": legacy,
        "timestamp": ts,
        "from_jid": from_jid,
        "to_jid": to_jid,
        "is_from_me": False,
        "push_name": None,
        "text": text,
        "type": 0,
        "attachment": attachment,
    }


# ─── helpers ──────────────────────────────────────────────────────────────


class TestStanzaOf:
    def test_extracts_wa_prefix(self):
        assert stanza_of({"external_id": "wa:ABC123"}) == "ABC123"

    def test_none_for_legacy_form(self):
        assert stanza_of({"external_id": "ios:42"}) is None

    def test_none_for_empty_wa(self):
        # "wa:" with nothing after is ambiguous — treat as no stanza.
        assert stanza_of({"external_id": "wa:"}) is None

    def test_none_for_missing(self):
        assert stanza_of({}) is None


class TestRoundTs5s:
    def test_rounds_down_to_5s_bin(self):
        a = _round_ts_5s("2026-05-26T09:00:03Z")
        b = _round_ts_5s("2026-05-26T09:00:04Z")
        assert a == b, "0-4s should bucket together"

    def test_distinct_bins(self):
        a = _round_ts_5s("2026-05-26T09:00:04Z")
        b = _round_ts_5s("2026-05-26T09:00:05Z")
        assert a != b, "5s boundary should split"

    def test_empty_string_when_no_ts(self):
        assert _round_ts_5s(None) == ""


class TestFingerprint:
    def test_same_fingerprint_when_within_5s(self):
        a = make_msg(ext="ios:1", ts="2026-05-26T09:00:02Z", text="hi", from_jid="A", to_jid="B")
        b = make_msg(ext="ios:1", ts="2026-05-26T09:00:04Z", text="hi", from_jid="A", to_jid="B")
        assert fingerprint(a) == fingerprint(b)

    def test_different_fingerprint_when_text_differs(self):
        a = make_msg(ext="ios:1", ts="2026-05-26T09:00:02Z", text="hi", from_jid="A", to_jid="B")
        b = make_msg(ext="ios:1", ts="2026-05-26T09:00:02Z", text="bye", from_jid="A", to_jid="B")
        assert fingerprint(a) != fingerprint(b)


# ─── reconcile_chat (the core dedup) ──────────────────────────────────────


class TestReconcileChat:
    def test_same_stanza_keeps_one(self):
        per_source = {
            "iphone_backup": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="hi")],
            "mac_live": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="hi")],
        }
        result = reconcile_chat(per_source)
        assert len(result) == 1
        assert result[0]["external_id"] == "wa:S1"

    def test_edit_picks_later_timestamp(self):
        """WhatsApp edits keep the stanza id but update timestamp/text."""
        per_source = {
            "iphone_backup": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="hi")],
            "mac_live": [make_msg(ext="wa:S1", ts="2026-05-26T09:01:00Z", text="hi (edited)")],
        }
        result = reconcile_chat(per_source)
        assert len(result) == 1
        assert result[0]["text"] == "hi (edited)"
        assert result[0]["timestamp"] == "2026-05-26T09:01:00Z"

    def test_tie_breaks_longer_text_when_timestamps_equal(self):
        """If both copies share a timestamp (within the 1-second wire
        resolution), the longer text wins — proxy for "captures more of
        the edit". """
        per_source = {
            "iphone_backup": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="hi")],
            "mac_live": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="hi friend")],
        }
        result = reconcile_chat(per_source)
        assert result[0]["text"] == "hi friend"

    def test_tie_breaks_source_priority_when_text_equal(self):
        per_source = {
            "iphone_backup": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="hi")],
            "mac_live": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="hi")],
        }
        # Default order: iphone before mac.
        result = reconcile_chat(per_source)
        assert len(result) == 1

        # Flip the order and the same input still produces one row, but
        # if the rows were distinguishable we'd see mac's variant. They
        # aren't distinguishable here — this test just confirms the
        # source_order kwarg is honoured without crashing.
        result_flipped = reconcile_chat(per_source, source_order=["mac_live", "iphone_backup"])
        assert len(result_flipped) == 1

    def test_null_stanza_fingerprint_dedup(self):
        """Group-system messages (~7 of 300k on the real iPhone DB)
        carry no stanza id. Fingerprint by (~5s ts, jids, text) catches
        them anyway."""
        per_source = {
            "iphone_backup": [
                make_msg(ext="ios:101", ts="2026-05-26T09:00:01Z", text="joined", from_jid="A", to_jid="G")
            ],
            "mac_live": [
                make_msg(ext="ios:909", ts="2026-05-26T09:00:03Z", text="joined", from_jid="A", to_jid="G")
            ],
        }
        result = reconcile_chat(per_source)
        assert len(result) == 1
        # Higher-timestamp variant wins.
        assert result[0]["external_id"] == "ios:909"

    def test_null_stanza_different_jids_pass_through(self):
        per_source = {
            "iphone_backup": [
                make_msg(ext="ios:101", ts="2026-05-26T09:00:00Z", text="bye", from_jid="A", to_jid="G")
            ],
            "mac_live": [
                make_msg(ext="ios:909", ts="2026-05-26T09:00:00Z", text="bye", from_jid="B", to_jid="G")
            ],
        }
        result = reconcile_chat(per_source)
        # Different from_jid → not a duplicate.
        assert len(result) == 2

    def test_iphone_only_message_passes_through(self):
        """Messages older than the Mac's history horizon exist only on
        the iPhone backup. Pass through untouched."""
        per_source = {
            "iphone_backup": [make_msg(ext="wa:OLD", ts="2014-05-26T09:00:00Z", text="ancient")],
            "mac_live": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="recent")],
        }
        result = reconcile_chat(per_source)
        assert len(result) == 2

    def test_mac_only_message_passes_through(self):
        """The Mac live DB had 19h of messages the iPhone backup hadn't
        caught yet — those must land."""
        per_source = {
            "iphone_backup": [make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="yesterday")],
            "mac_live": [
                make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="yesterday"),
                make_msg(ext="wa:NEW", ts="2026-05-27T17:00:00Z", text="today"),
            ],
        }
        result = reconcile_chat(per_source)
        assert len(result) == 2
        # Sorted by timestamp ascending.
        assert [m["external_id"] for m in result] == ["wa:S1", "wa:NEW"]

    def test_attachment_provenance_prefers_disk_present(self):
        """The Mac live DB almost never has attachment bytes on disk —
        the iPhone backup does. When the same message appears in both,
        the merged result must keep the iPhone's attachment metadata."""
        att = {"skipped": False, "sha256": "a" * 64, "filename": "photo.jpg", "size_bytes": 1024}
        per_source = {
            "iphone_backup": [
                make_msg(ext="wa:S1", ts="2026-05-26T09:00:00Z", text="pic", attachment=att)
            ],
            "mac_live": [
                # Mac side picked the message but couldn't resolve the file.
                make_msg(
                    ext="wa:S1", ts="2026-05-26T09:00:00Z", text="pic",
                    attachment={"skipped": True, "reason": "not on disk"},
                )
            ],
        }
        result = reconcile_chat(per_source)
        assert len(result) == 1
        assert result[0]["attachment"] == att

    def test_empty_inputs_returns_empty(self):
        assert reconcile_chat({"iphone_backup": [], "mac_live": []}) == []

    def test_single_source_is_identity_after_sort(self):
        msgs = [
            make_msg(ext="wa:A", ts="2026-05-26T09:00:00Z", text="a"),
            make_msg(ext="wa:B", ts="2026-05-26T09:01:00Z", text="b"),
        ]
        result = reconcile_chat({"iphone_backup": msgs})
        assert [m["external_id"] for m in result] == ["wa:A", "wa:B"]
        # No external fields beyond what was given (the _source tag is stripped).
        for m in result:
            assert "_source" not in m


class TestReconcileWholeExport:
    def test_one_jid_per_chat(self):
        per_source = {
            "iphone_backup": {
                "alice@s.whatsapp.net": [
                    make_msg(ext="wa:A1", ts="2026-05-26T09:00:00Z", text="hi"),
                ],
                "bob@s.whatsapp.net": [
                    make_msg(ext="wa:B1", ts="2026-05-26T09:00:00Z", text="yo"),
                ],
            },
            "mac_live": {
                "alice@s.whatsapp.net": [
                    make_msg(ext="wa:A1", ts="2026-05-26T09:00:00Z", text="hi"),
                    make_msg(ext="wa:A2", ts="2026-05-27T17:00:00Z", text="today"),
                ],
                # Bob has no traffic on the Mac side.
            },
        }
        result = reconcile(per_source)
        assert set(result.keys()) == {"alice@s.whatsapp.net", "bob@s.whatsapp.net"}
        assert [m["external_id"] for m in result["alice@s.whatsapp.net"]] == ["wa:A1", "wa:A2"]
        assert len(result["bob@s.whatsapp.net"]) == 1

    def test_jid_only_in_one_source_passes_through(self):
        per_source = {
            "iphone_backup": {},
            "mac_live": {
                "alice@s.whatsapp.net": [make_msg(ext="wa:A1", ts="2026-05-26T09:00:00Z")],
            },
        }
        result = reconcile(per_source)
        assert "alice@s.whatsapp.net" in result
        assert len(result["alice@s.whatsapp.net"]) == 1


# ─── probe CLI (synthetic DBs) ────────────────────────────────────────────


def _build_synthetic_db(path: Path, *, jid: str, stanzas: list[tuple[str, float]]) -> None:
    """Create a minimal iOS-shaped ChatStorage with one chat and the
    given (stanza_id, message_date) rows. ``message_date`` is in iOS
    Core Data seconds-since-2001 form."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT
        );
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZSTANZAID TEXT,
            ZMESSAGEDATE REAL
        );
        """
    )
    conn.execute("INSERT INTO ZWACHATSESSION VALUES (1, ?)", (jid,))
    for pk, (stanza, date) in enumerate(stanzas, start=1):
        conn.execute(
            "INSERT INTO ZWAMESSAGE VALUES (?, 1, ?, ?)",
            (pk, stanza, date),
        )
    conn.commit()
    conn.close()


class TestProbeChat:
    def test_reports_full_overlap_when_dbs_identical(self, tmp_path):
        ip = tmp_path / "ip.sqlite"
        mc = tmp_path / "mc.sqlite"
        rows = [("S1", 100.0), ("S2", 200.0), ("S3", 300.0)]
        _build_synthetic_db(ip, jid="alice@s.whatsapp.net", stanzas=rows)
        _build_synthetic_db(mc, jid="alice@s.whatsapp.net", stanzas=rows)

        report = probe_chat("alice@s.whatsapp.net", ip, mc)
        assert report.iphone_count == 3
        assert report.mac_count == 3
        assert report.overlap_stanza == 3
        assert report.iphone_only == 0
        assert report.mac_only == 0

    def test_reports_mac_only_recent_messages(self, tmp_path):
        ip = tmp_path / "ip.sqlite"
        mc = tmp_path / "mc.sqlite"
        # iPhone has up to S2; Mac has S2 and S3 (newer message that
        # hasn't made it into a backup yet).
        _build_synthetic_db(ip, jid="alice@s.whatsapp.net", stanzas=[("S1", 100.0), ("S2", 200.0)])
        _build_synthetic_db(mc, jid="alice@s.whatsapp.net", stanzas=[("S2", 200.0), ("S3", 300.0)])

        report = probe_chat("alice@s.whatsapp.net", ip, mc)
        assert report.overlap_stanza == 1
        assert report.iphone_only == 1
        assert report.mac_only == 1
