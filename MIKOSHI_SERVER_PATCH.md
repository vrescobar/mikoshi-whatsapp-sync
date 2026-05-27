# MIKOSHI_SERVER_PATCH — what landed, what's needed, what's out

This file used to be a hand-off doc telling the Mikoshi team what to
build server-side. The actual implementation landed in the Mikoshi
repo on branch `feat/ingest-committed-cursors`, and this file is now
the retro: **what's deployed**, **what each piece does**, and **what
the client expects** so both ends stay in sync.

## Endpoint 1 — `GET /api/ingest/v1/cursor`

### Status: **already shipped** on the Mikoshi server (note: singular `cursor`, not plural)

The Mikoshi team had already added this endpoint by the time the
client redesign landed. It lives at
`src/ingestion/routes/handleIngestCursor.ts` and surfaces the
per-chat watermarks for the authenticated account.

### Request

```http
GET /api/ingest/v1/cursor
Authorization: Bearer <ingest-token>
```

### Response (200 OK)

```json
{
  "account_id": "main",
  "cursors": [
    {
      "chat_jid": "1215643529322@lid",
      "last_external_id": "wa:STANZA-7",
      "last_message_at": "2026-05-26T07:14:17Z",
      "message_count": 3,
      "updated_at": "2026-05-26T07:14:18Z"
    }
  ]
}
```

The client parser (`pipeline_state._parse_cursors_payload`) also
accepts the older map-keyed shape (`{chats: {jid: {ts, external_id}}}`)
as a transition aid. The current Mikoshi uses the array shape above.

### Status codes

- `200` — success, return the body above. **Important:** return 200
  with `cursors: []` for "account exists but has no commits yet" — do
  not return 404 for that case (the client treats 404 as "server
  doesn't have this endpoint at all").
- `401` — invalid / missing / revoked token. The client decodes via
  `push_via_api.decode_auth_error` and surfaces a friendly hint.

## Endpoint 2 — `committed_cursors` in `POST /commit` response

### Status: **shipped in `feat/ingest-committed-cursors`** (branch on jetson, not yet merged to main)

The commit handler now returns an additional top-level field:

```json
{
  "ok": true,
  "stats": { "messagesInserted": 1247, "...": "..." },
  "committed_cursors": {
    "<jid>": {
      "ts": "2026-05-26T07:14:17Z",
      "external_id": "wa:STANZA-7"
    }
  }
}
```

The client writes those values into its local `.sync_state.json`
cache. The numeric counters stay under `stats` to keep the existing
wire shape stable.

### When `committed_cursors` is empty

The field is always present so the contract is stable. It's `{}` when:

- the commit is idempotent (same `push_id` seen twice — the second
  commit short-circuits via the "already committed" branch); or
- every message in the manifest was a duplicate (no cursor was
  moved).

Clients should treat "missing entries" as "nothing to update for those
JIDs", not as an error.

### Append-only invariant

The cursor handler refuses to retreat. If a commit brings messages
strictly older than the existing cursor (a backfill of long history),
the rows are inserted, but the cursor does not move backwards. The
server emits a structured warning (`refusing retrograde cursor
update`) and omits the affected JID from `committed_cursors`. Clients
should keep their own (higher) watermark in that case.

## Legacy external_id dedup

### Status: shipped in `feat/ingest-committed-cursors`

`ManifestMessageSchema` now accepts an optional `legacy_external_id`
field per message. Use case: the client is migrating from
`external_id="ios:<Z_PK>"` to `external_id="wa:<ZSTANZAID>"` and ships
both for messages it has already pushed in the old format. The commit
handler:

1. looks up `(account_id, platform_message_id = external_id)`. If
   present, dedup → skip.
2. otherwise looks up `(account_id, platform_message_id =
   legacy_external_id)`. If present, **update the existing row** in
   place to the new external_id and skip. Lazy migration — no
   backfill script needed.
3. otherwise insert with `external_id` as the primary key.

This means the wire transition can happen one row at a time as each
chat is re-synced from the new client. Server-side tests live in
`tests/apiIngestCommit.test.ts`.

## What is **explicitly out**

- **No server-side wipe / rewind endpoint.** A previous draft of this
  doc proposed an admin endpoint that would let the client purge or
  rewind a server-side cursor for one chat. The user has decided
  against shipping that — recovery scenarios that need it should be
  resolved by re-pushing the affected messages (server dedup handles
  the bytes), or by direct SQL on the jetson box if absolutely
  necessary. The endpoint is **not** implemented and **not** planned.

## Schema versioning

The client still sends `schema_version: "1.2"` in the manifest. All
changes here are additive (new response fields, optional manifest
field). No bump required; if a future change is non-additive, the
client and server pin a new literal version and the
`IngestionManifestSchema.parse` rejects mismatches early.

## Testing the round-trip

After deploying both endpoints, on the client:

```bash
./mikoshi-whatsapp.sh test-auth
# expect: "OK — https://your-mikoshi accepts this token (cursor endpoint reachable)."

./mikoshi-whatsapp.sh sync
# expect: "[OK] cursor cache updated from server (committed_cursors for N chats)"
```

If `committed_cursors` is missing from the commit response (very old
server), the client logs:

```
[OK] cursor cache updated from manifest (server didn't echo committed_cursors — old Mikoshi?)
```

That's the diagnostic for "endpoint 2 not deployed yet."
