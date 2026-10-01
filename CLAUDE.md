# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A macOS-hosted pipeline that reads WhatsApp's iOS Core Data DB (`ChatStorage.sqlite`), builds a JSON ingest manifest (schema v1.2, `whatsapp_export/schema.json`), and pushes it to a Mikoshi server's REST API (`/api/ingest/v1/*`). The server side lives in the Mikoshi repo, not here. The local `server/` directory is untracked legacy, and `MIKOSHI_SERVER_PATCH.md` documents the server endpoints the client expects.

All runtime code lives in `whatsapp_export/`. Modules import each other as top-level modules (`import pipeline_state`), so scripts run from inside that directory and tests put it on `sys.path` via `tests/conftest.py`.

## Commands

```bash
cd whatsapp_export
bash setup.sh                     # brew libimobiledevice + .venv + requirements
source .venv/bin/activate

# Tests (from repo root; pyproject sets testpaths and --import-mode=importlib)
python -m pytest -v
python -m pytest whatsapp_export/tests/test_extract.py -v
python -m pytest whatsapp_export/tests/test_extract.py::test_name -v

# Running the pipeline
./mikoshi-whatsapp.sh                       # TUI (tui.py)
./mikoshi-whatsapp.sh sync --skip-remote-sync   # extract only, no push (dry run)
./mikoshi-whatsapp.sh sync --chat-jid '<jid>' --since 2026-01-01
./mikoshi-whatsapp.sh status | test-auth | purge-extracted | verify-backup
python validate_export.py <export.json>     # schema validation (phase 4.5)
python reconciler.py ...                    # probe CLI: compare iPhone vs Mac DB for one chat

# macOS menu-bar app (Swift, no Xcode project)
cd whatsapp_export/gui && ./build.sh        # or ./install.sh
```

There is no configured linter.

## Architecture

**Phases** (`run_pipeline.sh` orchestrates them, and `mikoshi-whatsapp.sh` is the single entrypoint): 1 prepare/detect device → 2 `idevicebackup2` backup → 3a `selective_decrypt.decrypt_db_only` (ChatStorage only) → 3b scoped media decrypt → 4 `extract_messages.py` → 4.5 `validate_export.py` → 5 `push_via_api.py` (manifest → upload `needs_media[]` → `/commit`) → 6 GC of old exports. `pipeline_state.best_from_phase` picks the cheapest start phase from what's on disk. The TUI and the cron path both use it.

**Sources** (`sources/`): `iphone_backup` (decrypted backup: full history, real media) and `mac_live` (the Catalyst app's live DB under `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/`, opened with `mode=ro&immutable=1`; only about 1% of its media is on disk). Both use the same ZWAMESSAGE/ZWACHATSESSION/ZWAMEDIAITEM schema, so `extract_messages` is source-agnostic. When both sources are used, `extract_messages_multi_source` + `reconciler.py` dedup per chat: first by stanza id (`wa:` part of `external_id`), then by a fingerprint `(ts rounded to 5s, from_jid, to_jid, sha1(text))`. The iPhone attachment wins when the other side lacks the file.

**Cursor invariant (the core design rule, see REDESIGN.md):** the server's `ingestion_cursor` table is the source of truth for per-chat watermarks. `.sync_state.json` is only a cache:
- Only `push_via_api.py` writes it, and only after `/commit` returns 200, using the server's `committed_cursors` echo (`pipeline_state.update_cache_from_commit`).
- `extract_messages.py` reads it but never writes it, unless `MIKOSHI_TRUST_LOCAL_CURSOR=1` (legacy escape hatch).
- Tests that need multi-run cursor semantics must simulate the push step with `persist_cursors_like_push` in `conftest.py`.
- Drift (`IN_SYNC`, `LOCAL_AHEAD`, `SERVER_AHEAD`, `NO_SERVER_RECORD`, `NO_LOCAL_RECORD`) comes from `pipeline_state.detect_drift` against `GET /api/ingest/v1/cursors`.

**Manifest identity:** the server deduplicates on `external_id` (`wa:<ZSTANZAID>`) and `legacy_external_id` (`ios:<Z_PK>`). Never change how these are derived. Sync modes are `incremental` / `full` / `full-contact`.

**Extraction details:** Schema-dependent columns are probed at runtime (`table_exists`, `_resolve_media_size_expr`, `_message_group_member_expr`) because iPhone and Mac DB versions differ. Timestamps are Core Data seconds since 2001-01-01 (`IOS_EPOCH_OFFSET`). Attachments: videos and files over 5MB are dropped but still listed with `skipped: true` + `reason`. Media is sha256-keyed. System message types are filtered via `SYSTEM_MESSAGE_TYPES`.

**Other modules:** `favorites.py` (`~/.mikoshi-favorites.json`, matched by JID; cron syncs favorites if present), `scheduler.py` (LaunchAgent install/inspect), `tui_cache.py` (TUI header cache), `.pipeline.lock` (stale locks reclaimed automatically). `gui/` is a code-signed AppKit menu-bar app that exists so that Full Disk Access (needed to read `mac_live`) goes to one signed app instead of `/bin/bash`. Its child sync processes inherit the grant.

## Config and sensitive data

- `~/.mikoshi-ingest.conf`: `MIKOSHI_URL`, `MIKOSHI_TOKEN`, `MIKOSHI_PRESERVE_EXTRACTED`, `MIKOSHI_BACKUP_DIR`, etc. `MIKOSHI_SECURE_CLEANUP=1` shreds decrypted artifacts after a successful run.
- `exports/`, `logs/`, `temp/`, `.sync_state.json` and `.tui_cache.json` contain chat content and JIDs and are gitignored. Never commit them.
- `docs/USER_GUIDE.md` is the user-facing reference. `docs/design/` holds design notes, such as source reconciliation.
