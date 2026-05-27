# Sources and reconciliation

The client pulls WhatsApp messages from one or more sources, dedups
them, and emits a single manifest the server can dedupe again as
defense in depth. This doc explains the model — why there are two
sources, how the reconciler decides which copy wins, and what the
shape would be for a hypothetical third (e.g. WhatsApp Web).

## Why two sources

WhatsApp's two storage paths on this Mac:

| Source | What it is | Freshness | Coverage |
|---|---|---|---|
| `iphone_backup` | Decrypted `ChatStorage.sqlite` from an iPhone backup. The existing flow: pair the iPhone, run `idevicebackup2 backup`, run `iphone_backup_decrypt`, then read the `extracted/ChatStorage.sqlite`. | Lags by however long since the last backup. Typically hours to days. | Full message history the iPhone holds, including attachment bytes on disk under `extracted/Media/`. |
| `mac_live` | The live `ChatStorage.sqlite` written by WhatsApp's Mac Catalyst app at `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`. The Catalyst app keeps it in sync with the iPhone via WhatsApp Multi-Device. | Always current (writes happen in near real-time). | Only the messages the Catalyst app has been around for. Typically ~3× shorter history than the iPhone backup. **Almost no attachment bytes on disk** (Mac mostly stores thumbnails / cloud-fetch metadata). |

Neither dominates: the Mac has 15-20 hours of messages the iPhone
backup doesn't have yet; the iPhone has years of history the Mac
never saw. The reconciler's job is to merge both into one deduped
view.

## The schema is the same

Both sources speak the iOS Core Data schema (`ZWAMESSAGE`,
`ZWACHATSESSION`, `ZWAMEDIAITEM`). The `extract_messages.py`
module works against either with no code change — it just gets
handed a different `db_path`. That's why the multi-source path is a
thin wrapper: ``extract_messages_multi_source`` runs the single-source
extractor N times and reconciles the manifests.

## The dedup algorithm

`reconciler.reconcile_chat(per_source_msgs)` is run **per chat** and
applies the rules below in priority order. Strong matches first;
fuzzy matches only kick in when a stanza id is unavailable.

### 1. Stanza-id grouping (primary)

Each WhatsApp message has a stable protocol id (`ZSTANZAID`). The
same logical message has the same stanza id on every device, so this
is the load-bearing dedup signal.

The client exposes it in the manifest as the `wa:` prefix of
`external_id`, e.g. `wa:AC2A6DB0A92FDFAD48C15020A463371A`. Messages
sharing a stanza id are collapsed into one. Among the duplicates:

1. **Newer `timestamp` wins.** WhatsApp's 15-minute edit window keeps
   the stanza id but bumps the timestamp; the latest version is what
   we want.
2. **Non-empty text > empty text.** Belt-and-braces — should already
   be implied by (1) but cheap to enforce explicitly.
3. **Longer text wins on ties.** Proxy for "captures more of the
   edit."
4. **Source priority breaks the rest.** Default order:
   `iphone_backup` before `mac_live`. The iPhone is the practical
   media authority — its rows win when everything else is equal.

### 2. Fingerprint fallback (for null-stanza rows)

Around 7 messages in every 300k carry `ZSTANZAID = NULL` — mostly
group-system messages (member added/left, group icon changed). The
fingerprint is:

    (timestamp rounded to 5-second bin, from_jid, to_jid, sha1(text)[:12])

The 5-second bin absorbs clock skew between the iPhone and the Mac
(WhatsApp Multi-Device propagates these events with sub-second
latency, but device clocks can disagree by a second or two). Same
tie-break rules as stanza grouping.

### 3. Attachment provenance

The Mac live DB references attachments by stanza id but almost never
has the bytes on disk. The iPhone backup has both. When the same
message appears in both sources with different attachment states:

- If the chosen winner has attachment bytes on disk (`skipped: false`
  + `sha256`), keep its attachment.
- Otherwise, if the loser has attachment bytes, splice the loser's
  attachment metadata onto the winner.

This encodes "iPhone is the media authority" automatically — without
the client needing a separate "prefer iPhone for attachments" code
path.

### 4. Pass-through for messages unique to one source

Anything that doesn't match either dedup pass (same stanza or same
fingerprint) is unique and passes through. That's how the Mac's
fresh-but-Mac-only messages and the iPhone's old-but-iPhone-only
messages both make it into the merged manifest.

## Server-side dedup as defense in depth

The server runs its own `(account_id, platform_message_id)` dedup at
commit time. The client's reconciler exists so we don't *send* the
same message twice — sending dupes works (server filters them) but
wastes upload bandwidth and a real-world push of two iPhone+Mac
sources without dedup would be ~30% larger than needed.

## Probing the heuristic on real data

```
python3 -m reconciler probe \
    --jid <some-chat-jid> \
    --iphone /Volumes/models/iPhoneBackup/extracted/ChatStorage.sqlite \
    --mac "$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
```

Output for a typical group chat:

    chat:           120363425549264180@g.us
    iphone count:   470 stanzas (max ts 2026-05-26T20:28:47+00:00)
    mac count:      453 stanzas (max ts 2026-05-26T20:28:47+00:00)
    overlap:        426 (94.0% of the smaller)
    iphone only:    44
    mac only:       27

Numbers like these are the normal case. 0% overlap means something's
wrong (probably mismatched JIDs across the two installs). 100%
overlap on a chat that should have Mac-only newer messages means the
Mac DB isn't being written to (Catalyst app uninstalled / Multi-Device
not paired).

## Extending: a hypothetical third source

A new source (e.g. WhatsApp Web exporter) would need to:

1. Subclass `sources.base.Source` with:
   - `name` — short identifier (used in tie-break priority).
   - `is_available()` — can we read the source on this Mac?
   - `db_path()` — path to a ChatStorage.sqlite (or compatible
     schema). If the source isn't SQLite-shaped natively, this is
     where the conversion happens.
   - `media_root()` — directory for media bytes, or `None`.
2. Register it in `sources/__init__.py` (`SOURCES` registry).
3. The reconciler picks it up automatically. Tune its position in
   `source_order` if it should win or lose ties against existing
   sources.

WhatsApp Web is the obvious candidate but isn't currently used on
this Mac (no IndexedDB entries in any browser profile), so it's not
implemented today. The package layout leaves room for whenever it
becomes necessary.
