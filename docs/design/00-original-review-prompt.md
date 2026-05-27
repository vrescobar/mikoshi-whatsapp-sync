# Comprehensive UX + architecture review of mikoshi-whatsapp-sync

> Paste this into a fresh Claude Code session at the project root.
> Don't read code first — start by reading this prompt fully, then proceed.

## Project in 30 seconds

`mikoshi-whatsapp-sync` is a macOS pipeline that backs up an iPhone via
libimobiledevice, decrypts the WhatsApp `ChatStorage.sqlite`, extracts
messages to JSON, and pushes them to a self-hosted Mikoshi server (a
personal-AI / digital-twin system at `http://jetson:7777`). It's a
personal project, single user, no PII compromise concerns beyond the
user's own.

The 7-phase pipeline lives in `whatsapp_export/run_pipeline.sh`. A Python
TUI in `whatsapp_export/tui.py` wraps everything for interactive use.
Cron-driven non-interactive sync goes through `mikoshi-whatsapp.sh sync`.

### Phases (current)

1. `detect_device` — libimobiledevice `idevicebackup2`
2. `create_backup` — encrypted backup to `$MIKOSHI_BACKUP_DIR/backup/<UDID>` (incremental by default)
3. `decrypt_backup` — `selective_decrypt.py` → `extracted/ChatStorage.sqlite` (+ media)
4. `extract_and_export` — `extract_messages.py` → `exports/whatsapp_export_*.json`
4.5 schema validation
5. `secure_cleanup` — shred decrypted artifacts (opt-out via `MIKOSHI_PRESERVE_EXTRACTED`)
6. `sync_remote` — `push_via_api.py` → POST manifest / media / commit
7. `gc_local_exports` — keep last 5

Per-JID watermark stored in `whatsapp_export/.sync_state.json`. Server-side
idempotency via `external_id="ios:<Z_PK>"` + `client_id`.

## What's working today (don't redesign these)

- End-to-end pipeline runs and is theoretically incremental at every phase
- Selective per-chat decryption (`selective_decrypt.py`)
- Backup-tree corruption protection (validate Manifest.plist / SQLite header)
- macOS Keychain for backup password (`security` CLI)
- TUI's `_best_from_phase` auto-suggests which phase to start from based on disk state

## What's NOT working — pain points from a recent multi-hour debugging session

Read these carefully. The goal is to make them **impossible by design**,
not just patched.

### 1. Sync state drift (the silent killer)

`.sync_state.json` advances cursors based on **what `extract_messages.py`
extracted**, not on **what the server actually committed**. A real
sequence that broke the user:

1. User ran "Sync favorites" 4 times. Pipeline succeeded locally each
   time. Cursors advanced 4 times.
2. Server-side auth was misconfigured → every push 401'd. User didn't
   notice because the TUI showed "✓ Export validates".
3. User fixed auth. Re-ran "Sync favorites". Pipeline reported
   "0 messages, 0 attachments" — correct from the local cursor's POV,
   but the server had **zero** messages for that account.
4. Only recoverable by `rm .sync_state.json`.

### 2. No mental model

The TUI lists 14 actions. A user opening the TUI cold cannot tell:

- Whether they need to plug the iPhone or not
- Whether running "Sync favorites" again will resend duplicates
- Whether "Re-extract" loses data or is safe
- Whether the server already has what's about to be pushed
- The current relationship between **local state** and **server state**

### 3. Misleading labels

- "Full sync from iPhone" was actually *incremental* (fixed; was a UX bug)
- "Sync favorites now" defaulted to Phase 4 (no iPhone needed) even when
  iPhone was reachable and that would have been the right source
- `"0 messages, 0 attachments"` looks like a failure but is the success path

### 4. Tightly-coupled phases destroying each other's work

`secure_cleanup` (Phase 5) unconditionally `shred -vfz`'d ChatStorage.sqlite,
forcing a 13-min re-decrypt on every click. Patched — but the patch
proves the phases are coupled in non-obvious ways.

### 5. No "what's in the server" view

Local thinks it's synced; server has nothing; the user has to `ssh jetson
&& sqlite3` to find out. There's no `status --remote` command.

### 6. No "force re-sync this chat" path

User had a 419 MB JSON export with 967k messages from before any push
succeeded. There's no clear UX for "push this stale export to backfill
the server, then resume incremental from there". `"Push existing export
to Mikoshi"` exists but doesn't reconcile cursors afterward.

### 7. Multiple entry points doing similar things

`./mikoshi-whatsapp.sh tui`, `tui.py`, `run_pipeline.sh`, `push_via_api.py`
direct, `explore_backup.py extract` — overlapping capabilities, no
obvious "right" path. The TUI gradually accreted features without a
clear hierarchy.

### 8. Auth / account errors get a useless 401

When the Mikoshi account was disabled server-side, push returned
`401 unauthorized` with no actionable hint. User couldn't tell if it
was the token, the URL, the account, or a network issue.

### 9. Cron path vs interactive path diverge

The TUI has all the smart detection (`_best_from_phase`, etc.); the
cron-driven `sync` subcommand doesn't. A scheduled sync at 3am can't
recover from "iPhone unplugged" gracefully — it just fails the whole
thing.

---

## What I want from you (in order)

This is a multi-step review. **Do not propose code yet.** Propose the
redesign first.

### Step 1 — Read the codebase (~30 min)

Use the `Explore` agent in parallel if helpful. Key files:

- `whatsapp_export/tui.py` (~960 lines, the most-touched surface)
- `whatsapp_export/run_pipeline.sh`
- `whatsapp_export/extract_messages.py`
- `whatsapp_export/selective_decrypt.py`
- `whatsapp_export/push_via_api.py`
- `whatsapp_export/mikoshi-whatsapp.sh`
- `whatsapp_export/.sync_state.json` (current local state — read it)
- `README.md`
- Recent git log (`git log --oneline -30`) for what's been changing

### Step 2 — Map the user journeys

Enumerate **every distinct journey** the user might want to perform.
At least:

- First-run setup (no backup, no decrypt, no exports, no token)
- Daily incremental (iPhone plugged, want today's messages)
- Catch-up incremental (iPhone unplugged for a week, re-process from backup)
- Push one contact's full history (chosen by name or JID)
- Force re-sync (server lost data, or stale local cursor)
- Re-key (iPhone replaced → new UDID)
- Token rotation (server-side regenerated token)
- Cron-driven sync that gracefully handles "no iPhone available"
- Recover from "I pushed wrong data; need to undo" or "I deleted .sync_state.json"
- Inspecting what's already on the server without ssh

For each journey, document:
- What the user wants (one sentence)
- What the current TUI/CLI requires them to do (steps)
- What goes wrong / where they get confused
- What the **server-side observable outcome** is

Output as a markdown table.

### Step 3 — Redesign the state model

The fundamental bug is that `.sync_state.json` and the server's
`ingestion_cursor` table both claim to know "what's synced".

Propose a design where:
- Cursors advance **only on confirmed server commit**, never on local extraction alone
- The pipeline can detect drift between local and server cursors and surface it
- There is either a single source of truth (probably the server, with local cache mirroring it after each successful commit) or explicit reconciliation
- A failed push doesn't silently leave cursors lying

Diagrams welcome (ASCII / mermaid).

### Step 4 — Redesign the TUI mental model

Propose a new menu structure built around the **user's intent**, not the
pipeline's phases. Show:

- A wireframe of the new top-level menu (≤ 7 options, ideally 4–5)
- The "status header" the user sees on entering: their current state
  (e.g. `iPhone last seen: 2 min ago · Local cursor: 2026-05-26 18:42 · Server cursor: 2026-05-25 09:13 · 4 chats out of sync`)
- The state machine the TUI maintains (what's known, what's stale)

### Step 5 — Propose phase reorganization

Are 7 phases right? Specifically consider:

- Should "extract" and "push" be merged so cursors only advance after push succeeds?
- Should "decrypt" be split into "decrypt-DB-only" and "decrypt-media-only"?
- Should `secure_cleanup` be opt-in instead of opt-out? (Currently opt-out, was wrecking 13 min of work.)
- Where do retries / partial failures live? (e.g. push fails halfway → which phase owns the retry?)

### Step 6 — Concrete deliverables

Produce a **single markdown doc** named `REDESIGN.md` at the project root,
with the following structure:

1. Executive summary (≤ 200 words: what you'd change, why)
2. The 9 pain points → which are TUI bugs, which are architectural
3. User journeys table (Step 2)
4. New state model (Step 3) with diagrams
5. New TUI (Step 4) with wireframes
6. Phase reorganization (Step 5)
7. **Migration plan** — staged, doesn't break working installs.
   Include backwards-compatible env vars / config keys.
8. Files-to-change table, ranked by impact (highest first), with
   estimated LOC delta and risk level (low/medium/high)
9. Open questions (anything you couldn't decide; flag them so the user
   can choose)

### Step 7 — Be opinionated, justify, don't over-engineer

- Where the current design is fine, **say so and don't fix it**.
  (E.g. macOS Keychain integration is correct; don't redesign it.)
- Where it's wrong, say **why** — the user is a sharp engineer and
  wants the reasoning, not just the verdict.
- Don't propose rewriting from scratch unless absolutely necessary.
  This is a personal-use tool that mostly works; the goal is to fix the
  mental-model gap and the state-drift footgun, not to ship v2.
- If you find a problem I didn't list, surface it.

## Constraints

- Single user, macOS only
- iPhone backups are 96 GB+ on an external SSD — decryption is expensive (~13 min) and must remain cacheable
- The Mikoshi server is a personal jetson box, the user controls it; both ends can be modified if the redesign needs server-side changes (but flag those clearly so they can be done as a separate PR on the server repo)
- Bash 3.2 portability matters (default macOS bash)
- Python 3.11+ available via `.venv`
- No major new dependencies — `questionary`, `rich`, `iphone_backup_decrypt`, `urllib` is the current set

## Time budget

~30–45 min of reading + ~60–90 min of writing the redesign. If you blow
past 2 hours of wall time, deliver what you have and flag what's still
open. Don't write code yet — the goal of this session is the design doc.

Start by **listing the files you're going to read and what you're looking
for in each**. Then proceed.
