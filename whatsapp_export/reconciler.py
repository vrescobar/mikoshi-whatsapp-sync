"""Cross-source message reconciler.

When the sync pipeline pulls messages from more than one source (today:
the iPhone backup and the Mac Catalyst live DB), each chat is going to
get duplicate rows for any message that exists on both sides. This
module deduplicates them before the manifest is built.

The dedup algorithm runs **per chat** and prioritises strong matches
over fuzzy ones:

1. **Stanza-id match.** Messages with the same WhatsApp protocol id
   (``ZSTANZAID`` on the row, exposed as the ``wa:`` part of the
   manifest's ``external_id``) collapse into one. The winner is the
   message with the highest ``timestamp``; on tie, the longer ``text``
   wins (catches WhatsApp edits — same stanza, newer text).
2. **Fingerprint match** (for rows where stanza is null, ~7 of 300k in
   the wild). Key: ``(timestamp_rounded_to_5s, from_jid, to_jid,
   sha1(text))``. Same fingerprint → same message. Same tie-break as
   above.
3. **Attachment provenance.** When the chosen winner has no attachment
   on disk but a same-stanza/same-fingerprint sibling does, the
   sibling's attachment metadata replaces the winner's. iPhone backup
   is the practical media authority — this rule encodes that
   automatically.

The function ``reconcile_chat`` is pure and exhaustively unit-tested.
A ``probe`` CLI exercises the real iPhone and Mac DBs side-by-side so
the heuristic can be sanity-checked against live data before a sync.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ─── stanza extraction ────────────────────────────────────────────────────


def stanza_of(msg: dict) -> str | None:
    """Extract the stanza id from a manifest-shaped message dict.

    Source of truth is the ``external_id`` field, formatted as
    ``wa:<STANZAID>``. Falls back to ``None`` when external_id is the
    legacy ``ios:<Z_PK>`` form (no stanza available).
    """
    ext = msg.get("external_id") or ""
    if ext.startswith("wa:"):
        return ext[3:] or None
    return None


# ─── fingerprint (for stanza=null rows) ───────────────────────────────────


def _round_ts_5s(ts: str | None) -> str:
    """Round an ISO-8601 timestamp to the nearest 5 seconds.

    Absorbs clock skew between the iPhone and the Mac for messages
    that come in without a stanza id. 5 seconds is small enough that
    distinct messages (typed at human speed) don't accidentally
    collide; large enough to absorb the device-clock drift typical of
    Multi-Device sync.
    """
    if not ts:
        return ""
    # Parse the ISO without a hard timezone dependency. We just need a
    # stable bucket — round to whole 5-second bins.
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts  # opaque string fallback
    epoch = int(dt.timestamp())
    return str(epoch - (epoch % 5))


def fingerprint(msg: dict) -> tuple[str, str, str, str]:
    """Stable bucket for messages that lack a stanza id."""
    text_hash = hashlib.sha1((msg.get("text") or "").encode("utf-8")).hexdigest()[:12]
    return (
        _round_ts_5s(msg.get("timestamp")),
        (msg.get("from_jid") or ""),
        (msg.get("to_jid") or ""),
        text_hash,
    )


# ─── per-message scoring ──────────────────────────────────────────────────


def _is_attachment_present(att: dict | None) -> bool:
    """Did this message land with an attachment we have bytes for?

    The manifest schema marks dropped attachments as ``skipped: true``
    or ``None``. Only ``skipped: false`` rows with a ``sha256``
    indicate "bytes available on disk under our attachments dir".
    """
    return bool(att and att.get("skipped") is False and att.get("sha256"))


def _pick_better(a: dict, b: dict, source_order: list[str]) -> dict:
    """Tie-break: among two same-id messages, pick which to keep.

    Rules in priority order (matches docstring of ``reconcile_chat``):
      1. Newer ``timestamp`` wins (edits → keep latest text).
      2. Non-empty text > empty text.
      3. Longer text wins (WhatsApp edits typically only extend).
      4. Higher-priority source wins (iphone_backup before mac_live —
         iPhone is the practical authority for completeness).
    """
    ta = a.get("timestamp") or ""
    tb = b.get("timestamp") or ""
    if ta != tb:
        return a if ta > tb else b

    tx_a = a.get("text") or ""
    tx_b = b.get("text") or ""
    if bool(tx_a) != bool(tx_b):
        return a if tx_a else b
    if len(tx_a) != len(tx_b):
        return a if len(tx_a) > len(tx_b) else b

    sa = a.get("_source", "")
    sb = b.get("_source", "")
    ia = source_order.index(sa) if sa in source_order else len(source_order)
    ib = source_order.index(sb) if sb in source_order else len(source_order)
    return a if ia <= ib else b


def _merge_attachment(winner: dict, loser: dict) -> dict:
    """When the winner has no attachment bytes but the loser does,
    splice the loser's attachment metadata onto the winner."""
    if _is_attachment_present(winner.get("attachment")):
        return winner
    if _is_attachment_present(loser.get("attachment")):
        return {**winner, "attachment": loser["attachment"]}
    return winner


# ─── chat-level reconcile ─────────────────────────────────────────────────


def reconcile_chat(
    per_source: dict[str, list[dict]],
    source_order: list[str] | None = None,
) -> list[dict]:
    """Merge the message lists from one chat across N sources.

    Returns a single deduped + sorted list (oldest first). Each input
    list must be the manifest-shape produced by ``extract_messages``
    (``external_id``, ``timestamp``, ``text``, ``from_jid``, etc.).

    ``per_source`` is keyed by source name (e.g. ``"iphone_backup"``,
    ``"mac_live"``) so the reconciler can recall provenance for
    tie-breaks and attachment merging. ``source_order`` controls
    tie-break priority: earlier entries win on otherwise-equal
    messages. Default order favours ``iphone_backup`` because the
    iPhone is the practical media authority.
    """
    if source_order is None:
        source_order = ["iphone_backup", "mac_live"]

    # Tag each message with its source so tie-breakers see it.
    all_messages: list[dict] = []
    for source_name, msgs in per_source.items():
        for m in msgs:
            all_messages.append({**m, "_source": source_name})

    # Pass 1: group by stanza_id when present.
    by_stanza: dict[str, list[dict]] = defaultdict(list)
    no_stanza: list[dict] = []
    for m in all_messages:
        s = stanza_of(m)
        if s is None:
            no_stanza.append(m)
        else:
            by_stanza[s].append(m)

    merged: list[dict] = []
    for group in by_stanza.values():
        winner = group[0]
        for m in group[1:]:
            chosen = _pick_better(winner, m, source_order)
            other = m if chosen is winner else winner
            winner = _merge_attachment(chosen, other)
        merged.append(winner)

    # Pass 2: fingerprint-dedup the stanza=null tail.
    by_fp: dict[tuple, list[dict]] = defaultdict(list)
    for m in no_stanza:
        by_fp[fingerprint(m)].append(m)
    for group in by_fp.values():
        winner = group[0]
        for m in group[1:]:
            chosen = _pick_better(winner, m, source_order)
            other = m if chosen is winner else winner
            winner = _merge_attachment(chosen, other)
        merged.append(winner)

    # Strip the provenance tag before returning so the result is
    # manifest-shaped again.
    for m in merged:
        m.pop("_source", None)

    # Stable sort by timestamp; rows without a timestamp go last in
    # insertion order (rare — only synthetic test data).
    merged.sort(key=lambda m: (m.get("timestamp") or "9999"))
    return merged


def reconcile(
    per_source: dict[str, dict[str, list[dict]]],
    source_order: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Whole-export reconcile.

    ``per_source[source_name][jid]`` = list of manifest-shaped message
    dicts. Returns ``{jid: merged_messages}`` — one deduped list per
    chat that appeared in any source.
    """
    all_jids: set[str] = set()
    for source_data in per_source.values():
        all_jids.update(source_data.keys())

    out: dict[str, list[dict]] = {}
    for jid in all_jids:
        per_chat = {
            sname: per_source[sname].get(jid, []) for sname in per_source
        }
        out[jid] = reconcile_chat(per_chat, source_order)
    return out


# ─── probe CLI ────────────────────────────────────────────────────────────


@dataclass
class ProbeReport:
    iphone_count: int
    mac_count: int
    overlap_stanza: int
    iphone_only: int
    mac_only: int
    iphone_max_ts: str | None
    mac_max_ts: str | None


def probe_chat(jid: str, iphone_db: Path, mac_db: Path) -> ProbeReport:
    """Compare the two live DBs for one chat without touching the
    manifest pipeline. Useful for sanity-checking the dedup heuristic
    on real data before the user runs a sync.
    """
    def _q(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    ip = _q(iphone_db)
    mc = _q(mac_db)
    try:
        chat_pk_ip = ip.execute(
            "SELECT Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID = ?", (jid,)
        ).fetchone()
        chat_pk_mc = mc.execute(
            "SELECT Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID = ?", (jid,)
        ).fetchone()
        if chat_pk_ip is None and chat_pk_mc is None:
            raise SystemExit(f"chat {jid} not present in either source")

        def _stanzas(conn: sqlite3.Connection, chat_pk: int | None) -> tuple[set[str], str | None]:
            if chat_pk is None:
                return set(), None
            rows = conn.execute(
                "SELECT ZSTANZAID, ZMESSAGEDATE FROM ZWAMESSAGE WHERE ZCHATSESSION = ?",
                (chat_pk,),
            ).fetchall()
            ids = {r["ZSTANZAID"] for r in rows if r["ZSTANZAID"]}
            max_ts = None
            if rows:
                max_raw = max((r["ZMESSAGEDATE"] or 0) for r in rows)
                from datetime import datetime, timezone
                # Core Data epoch: seconds since 2001-01-01.
                max_ts = datetime.fromtimestamp(978307200 + max_raw, tz=timezone.utc).isoformat()
            return ids, max_ts

        ip_ids, ip_max = _stanzas(ip, chat_pk_ip["Z_PK"] if chat_pk_ip else None)
        mc_ids, mc_max = _stanzas(mc, chat_pk_mc["Z_PK"] if chat_pk_mc else None)

        overlap = len(ip_ids & mc_ids)
        return ProbeReport(
            iphone_count=len(ip_ids),
            mac_count=len(mc_ids),
            overlap_stanza=overlap,
            iphone_only=len(ip_ids - mc_ids),
            mac_only=len(mc_ids - ip_ids),
            iphone_max_ts=ip_max,
            mac_max_ts=mc_max,
        )
    finally:
        ip.close()
        mc.close()


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_probe = sub.add_parser(
        "probe",
        help="Compare iPhone backup DB and Mac live DB for one chat (read-only)",
    )
    p_probe.add_argument("--jid", required=True)
    p_probe.add_argument("--iphone", type=Path, required=True)
    p_probe.add_argument("--mac", type=Path, required=True)
    p_probe.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    if args.cmd == "probe":
        report = probe_chat(args.jid, args.iphone, args.mac)
        if args.json:
            print(json.dumps(report.__dict__, indent=2))
        else:
            print(f"chat:           {args.jid}")
            print(f"iphone count:   {report.iphone_count} stanzas (max ts {report.iphone_max_ts})")
            print(f"mac count:      {report.mac_count} stanzas (max ts {report.mac_max_ts})")
            print(f"overlap:        {report.overlap_stanza} ({_pct(report.overlap_stanza, min(report.iphone_count, report.mac_count))} of the smaller)")
            print(f"iphone only:    {report.iphone_only}")
            print(f"mac only:       {report.mac_only}")
        return 0

    return 0


def _pct(num: int, denom: int) -> str:
    if not denom:
        return "n/a"
    return f"{100 * num / denom:.1f}%"


if __name__ == "__main__":
    sys.exit(_main())
