# MIKOSHI_SERVER_PATCH — companion PR for the Mikoshi repo

The client redesign in this repo is fully usable against an existing
Mikoshi server — push still works, cursors still get persisted (via the
`extracted-offline` fallback path in `push_via_api.py`). But two small
endpoints on the server side unlock the *full* benefit of the redesign:
authoritative cursor reads, sharper drift detection, and a faster plan
screen.

This doc describes exactly what to change on the server side. It belongs
as a separate PR in the Mikoshi repo (`src/ingestion/`). The client
detects whether either endpoint is present and degrades silently when
they aren't — so the two PRs can be merged in either order.

## Endpoint 1 — `GET /api/ingest/v1/cursors`

### Purpose

Let the client ask "what's the last message you have, per chat?" without
having to query a SQL shell on the server box. Eliminates the silent
drift bug — instead of trusting its local cache, the client uses the
server's view as authoritative every time it builds a sync plan.

### Request

```http
GET /api/ingest/v1/cursors
Authorization: Bearer <ingest-token>
```

No query parameters. The token resolves to an account; the response is
scoped to that account.

### Response (200 OK)

```json
{
  "version": "1.2",
  "account_id": "u_01HXY...",
  "as_of": "2026-05-26T18:42:00Z",
  "chats": {
    "<jid>": {
      "ts": "2026-05-26T07:14:17Z",
      "external_id": "ios:1834219"
    },
    ...
  }
}
```

The client also accepts the flattened shape `{"<jid>": {...}}` (no
envelope) — handy for the simplest possible implementation.

Fields:

- `ts` — ISO-8601 UTC timestamp of the latest committed message for this
  chat. Used as the lower bound for the next incremental extraction.
- `external_id` — `"ios:<Z_PK>"`. The strong identity — when the server
  re-receives a message with this external_id it dedups and skips.

### Status codes

- `200` — success, return the JSON body above.
- `401` — invalid / missing / revoked token. The client surfaces a
  decoded hint (see `decode_auth_error` in `push_via_api.py`); the body
  format the client tries to parse is anything containing the words
  `token`, `account`, `disabled`, `expired`, `revoked`, etc.
- `404` — endpoint not deployed. The client treats this as "old Mikoshi"
  and falls back silently. **Don't accidentally return 404 for "account
  has no commits yet" — return 200 with `chats: {}` instead.**

### Suggested SQL

Assuming a `messages` table with `(account_id, chat_jid, timestamp,
external_id)` columns and an index on `(account_id, chat_jid, timestamp DESC)`:

```sql
SELECT chat_jid,
       MAX(timestamp) AS ts,
       (
         SELECT external_id
           FROM messages m2
          WHERE m2.account_id = m.account_id
            AND m2.chat_jid = m.chat_jid
          ORDER BY m2.timestamp DESC, m2.id DESC
          LIMIT 1
       ) AS external_id
  FROM messages m
 WHERE account_id = :account_id
 GROUP BY chat_jid;
```

Or if you already have an `ingestion_cursor` (or similar) table that
tracks the watermark at write time — that's even better; just project
its rows.

### Performance

The client polls this once per TUI open and once per sync run (throttled
to 30s in the TUI). It should return in <100ms for an account with a few
hundred chats. Add an index if it doesn't.

## Endpoint 2 — `committed_cursors` in `POST /commit` response

### Purpose

Today the commit response is `{stats: {...}}`. The new client wants the
server to *also* echo back the per-chat watermarks it just advanced.
That's the value the client writes into its local cache — it's the
strongest possible guarantee that local state matches server state,
because it comes straight from the server's own database after the write
succeeded.

### Response (200 OK) — addition

The existing `stats` field stays exactly as it is. Add:

```json
{
  "stats": { ... existing fields ... },
  "committed_cursors": {
    "<jid>": {
      "ts": "2026-05-26T07:14:17Z",
      "external_id": "ios:1834219"
    },
    ...
  }
}
```

The server already computes the max-per-chat to advance its own cursor
inside the commit handler — just echo that data back instead of
discarding it.

### When `committed_cursors` should be present

Only for chats whose cursor was *changed* by this commit. Echoing the
unchanged ones is fine but adds bytes; the client tolerates either.

### Client fallback when missing

When the field is absent (old server), the client computes cursors
locally from the manifest it just successfully pushed and tags them with
`source: "extracted (offline)"`. The next drift check re-verifies them
against the server. So this change is a quality-of-signal improvement,
not a correctness requirement.

## Endpoint 3 (optional) — `POST /api/ingest/v1/cursors/rewind`

### Purpose

Power-user recovery: rewind the server-side cursor for one chat so a
re-push picks up messages older than the current watermark.

Today the same effect is achievable by re-submitting the older messages
with their original `external_id` values — the server's dedup is by
external_id, not by timestamp range, so the bytes still land. But that
requires re-running the client with `--mode full-contact` and ignoring
the cursor (which the redesign no longer lets the client do trivially).

A rewind endpoint cuts this to a single click in the TUI's `Tools →
Rewind cursor for one chat`.

### Request

```http
POST /api/ingest/v1/cursors/rewind
Authorization: Bearer <ingest-token>
Content-Type: application/json

{
  "chat_jid": "34xxxxxxxxx@s.whatsapp.net",
  "to_ts": "2026-01-01T00:00:00Z"  // rewind to this timestamp; null = full rewind
}
```

### Response (200 OK)

```json
{ "ok": true, "new_cursor_ts": "2026-01-01T00:00:00Z" }
```

### Authorization

Same Bearer token. Optionally gate behind a separate scope/role if the
ingest token is supposed to be append-only.

### Notes

- This must not delete any committed messages; it only moves the cursor
  back. The next client push re-submits messages, which dedup as no-ops.
- Audit-log every rewind. Single-user tools shouldn't need this often;
  if you see frequent rewinds, something else is wrong.

## Schema versioning

The client still sends `schema_version: "1.2"` in the manifest, and the
server still pins-checks that exact string. None of the additions here
require a schema bump — they're new endpoints and additive response
fields. If the server wants to advertise capability:

```http
GET /api/ingest/v1/capabilities
→ { "schema_versions": ["1.2"], "endpoints": ["cursors", "cursors-rewind", "commit-echo"] }
```

Optional. The client doesn't probe this today; it just tries the
endpoint and degrades on 404.

## Testing the round-trip

After deploying both endpoints, on the client:

```bash
./mikoshi-whatsapp.sh test-auth
# expect: "OK — https://your-mikoshi accepts this token (cursors endpoint reachable)."

./mikoshi-whatsapp.sh status
# expect: "Server: ✓ <url>  N chats tracked   last commit <ts>"

./mikoshi-whatsapp.sh sync
# expect: cursor cache updated from server response (committed_cursors for N chats)
```

If `committed_cursors` is missing from the commit response the client
logs:

```
[OK] cursor cache updated from manifest (server didn't echo committed_cursors — old Mikoshi?)
```

That's the diagnostic for "endpoint 2 not deployed yet."
