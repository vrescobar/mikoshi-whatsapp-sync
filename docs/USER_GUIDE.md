# Mikoshi WhatsApp Sync — User Guide

This is the step-by-step "how do I actually use this" doc. For the
architecture, see `docs/design/`; for a one-screen cheatsheet, run the
TUI and pick *📚 Help*.

## What this is

A pipeline that pulls WhatsApp messages from this Mac and ships them
to a self-hosted [Mikoshi](https://mikoshi.example) server. Two data
sources are supported and automatically reconciled:

| Source          | Where it lives | Freshness | History | Media bytes |
|---|---|---|---|---|
| `iphone_backup` | A decrypted iPhone backup at `${MIKOSHI_BACKUP_DIR}/extracted/ChatStorage.sqlite` | Updated when you run a backup (hours / days lag) | Full | Yes — bytes on disk |
| `mac_live`      | `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite` (Mac Catalyst WhatsApp app) | Continuous (near-real-time) | Only since the Mac was linked to WhatsApp Multi-Device | Mostly thumbnails / cloud-fetch metadata |

The reconciler dedups by stanza id (stable across devices), so feeding
both sources doesn't double-count messages.

## First-time setup

1. **Install dependencies.** `cd whatsapp_export && bash setup.sh`. This
   creates a `.venv` and installs the Python deps.

2. **Configure the server target.** Create `~/.mikoshi-ingest.conf` with
   at least:
   ```
   MIKOSHI_URL=https://your-mikoshi.example.com
   MIKOSHI_TOKEN=paste-token-here
   ```
   Optional: `MIKOSHI_BACKUP_DIR` (where to put iPhone backups),
   `MIKOSHI_CLIENT_ID`, `KEEP_LOCAL_EXPORTS`,
   `MIKOSHI_PRESERVE_EXTRACTED=true`.

3. **Validate.** Run `./mikoshi-whatsapp.sh test-auth` — should print
   "OK — <url> accepts this token (cursor endpoint reachable)."

4. **Open the TUI.** `./mikoshi-whatsapp.sh` (no args). The header
   shows the state of each source, the server, and any sync drift.

## Daily workflow

### Manual sync

```
./mikoshi-whatsapp.sh sync
```

With no flags this does the right thing:
- Picks favorites if `~/.mikoshi-favorites.json` exists, else all chats.
- Picks both sources if both are available, falls back to whichever is.
- Skips iPhone backup phases if a cached backup is already on disk.
- Exits cleanly (rc=0) when there's nothing to sync — safe for cron.

Useful flags:
- `--all` ignore favorites and sync every chat
- `--skip-remote-sync` extract locally but don't push (dry run)
- `--sources iphone_backup` or `--sources mac_live` force a specific
  source instead of auto-detect

### Scheduled sync (recommended)

Open the TUI, pick **⏰ Schedule automatic sync**, enable, pick a daily
time. The TUI installs a LaunchAgent at
`~/Library/LaunchAgents/com.mikoshi.sync.plist`. From then on, your
Mac runs the sync once a day at the chosen time — no terminal needed.

To stop it, open the same menu and pick **🔴 Disable**.

## Inspecting state

### TUI header (always visible)

The header shows, in order:

- **iPhone** — is the device reachable over USB right now?
- **Backup** — `MIKOSHI_BACKUP_DIR` path + how full it is
- **Decrypt** — is there a usable decrypted ChatStorage on disk?
- **Server** — Mikoshi reachability + chat count tracked
- **Sources** — which data sources are available + msg counts
- **State** — drift summary (`in-sync` / `N never-pushed` / etc.)

### Inspect screen

`./mikoshi-whatsapp.sh` → **📊 Inspect** for:

- List of all local chats (sorted by last-message-date)
- Per-chat drift between local cache and server cursor
- Pipeline status (config, backup, sync state)

## Post-sync verification

Every sync ends with a result Panel:

- **✓ Sync confirmed** — server-side commit count matches plan estimate.
- **⚠ Mismatch** — server saw fewer messages than expected. Cause is
  usually cross-source dedup (the plan estimate is an upper bound;
  stanza-id overlap between sources means the server legitimately
  dedups some).
- **✗ Sync failed** — non-zero pipeline exit code; check the log.
- **ℹ Sync OK; nothing pushed** — `--skip-remote-sync` was on.

## When things go wrong

- **"server cursor unreachable"** — Mikoshi is down or unreachable
  from this Mac. Sync refuses to run rather than silently drifting.
  Set `MIKOSHI_TRUST_LOCAL_CURSOR=1` only when you understand the
  drift risk.
- **"no iPhone reachable and no cached backup"** — Plug the iPhone in,
  unlock it, and trust this Mac. Or use Mac-only sync via the TUI
  source picker (no iPhone required).
- **"ChatStorage corrupt (bad SQLite header)"** — A killed decrypt run
  left a partial file. `rm` it and re-run with `--from-phase 3`.
- **Cron job not firing** — Check
  `logs/launchagent.{out,err}.log`. The LaunchAgent runs at the
  configured time only when the Mac is awake; sleep at the time slot
  means the next wakeup picks it up.

## Reference

- One-screen cheatsheet inside the TUI: **📚 Help**
- Source model: `docs/design/sources-and-reconciliation.md`
- Server-side cursor design: `docs/design/accounts.md`
- Wrapper flag reference: `./mikoshi-whatsapp.sh --help`
