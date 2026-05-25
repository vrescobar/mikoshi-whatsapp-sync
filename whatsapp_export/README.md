# WhatsApp Export Pipeline for Mikoshi

Automated, secure pipeline to extract WhatsApp conversation history from iPhone for training a digital twin model.

**Status**: WiFi Sync enabled on device, data extraction from encrypted backup, secure local processing, remote sync via rsync+SSH.

## Overview

This pipeline:
1. 🔍 **Detects** iPhone on WiFi/USB
2. 🔐 **Creates** encrypted backup locally (temporary)
3. 🔑 **Decrypts** using iPhone backup password from macOS Keychain
4. 📊 **Extracts** ChatStorage.sqlite + WhatsApp media
5. 📝 **Exports** conversations to structured JSON following [schema.json](schema.json)
6. 📎 **Filters & copies** attachments (images / audio / docs only, ≤ 5MB) into `exports/attachments/`
7. 🗑️  **Securely deletes** all sensitive files (7-pass shred)
8. 📤 **Syncs** JSONs + attachments to Mikoshi server via rsync

### Sync modes

| Mode | Flag | Behavior |
|---|---|---|
| Incremental (default) | _(none)_ | Only messages newer than last sync per chat. Updates `.sync_state.json`. |
| Full re-sync | `--mode full` | Re-pulls every chat from the beginning. Resets `.sync_state.json`. |
| Per-contact re-sync | `--mode full-contact --contact "Alice"` | Re-pulls one contact / group entirely. Other chats keep their incremental state. |

State is persisted in `.sync_state.json` (last message timestamp per JID). The server-side processor must upsert by `message.id` (stable `ZWAMESSAGE.Z_PK`) to handle overlap between modes.

### Attachment filters

- ✂️ Videos rejected (any size, any extension)
- ✂️ Anything > 5MB rejected
- ✅ Kept: images, audio, PDFs, Office docs, plain text
- Skipped attachments still appear in JSON with `skipped: true` + `reason`, so the server can show "[video filtered]" instead of silently missing data.

### Security Features

- ✅ **No hardcoded passwords** - uses macOS Keychain
- ✅ **No iCloud sync** - local processing only
- ✅ **Secure deletion** - 7-pass shred for ChatStorage.sqlite, backup.plist
- ✅ **Idempotent** - re-runnable without data duplication
- ✅ **Logging** - timestamps, no secrets in logs
- ✅ **SSH key auth** - rsync via SSH, not password-based

## Requirements

- **macOS** 10.13+ with WiFi Sync enabled on iPhone
- **Homebrew** (for libimobiledevice)
- **Python 3.8+**
- **SSH key pair** for remote server (optional but recommended)

## Directory Structure

```
whatsapp_export/
├── setup.sh                       # Install dependencies
├── run_pipeline.sh                # Orchestrator (bash)
├── extract_messages.py            # Extraction + filters + sync state (Python)
├── schema.json                    # JSON Schema consumed by Mikoshi server
├── verify_setup.sh                # Pre-flight checker
├── README.md / QUICKSTART.md
├── .sync_state.json               # Last sync timestamp per chat (auto-created)
├── logs/
│   └── pipeline_20260525_143015.log
├── exports/                       # Synced to server
│   ├── whatsapp_export_20260525_143015.json
│   └── attachments/               # Deduped by sha256
│       ├── 3f5a...e1.jpg
│       └── 8b21...c4.pdf
└── temp/                          # Wiped after each run
```

**Note**: `backups/` directory is NOT created. All backups are temporary and deleted after extraction to maximize security.

## Initial Setup

### 1. Enable WiFi Sync on iPhone

```
iPhone Settings → General → AirDrop & Handoff → WiFi Sync → Enable
```

Ensure your iPhone and Mac are on the same WiFi network.

### 2. Install Dependencies

```bash
cd ~/projects/mikoshi-whatsapp-sync/whatsapp_export
bash setup.sh
```

This installs:
- `libimobiledevice` (device communication)
- `usbmuxd` (USB/WiFi bridge)
- Python packages: `iphone-backup-decrypt`, `whatsapp-chat-exporter`, `cryptography`

### 3. Store iPhone Backup Password in Keychain

Get your iPhone backup password (usually created when first backing up to this Mac):

```bash
security add-generic-password \
  -a iphone_backup \
  -s iphone_backup_password \
  -w 'YOUR_BACKUP_PASSWORD' \
  2>/dev/null
```

To verify it was stored:
```bash
security find-generic-password -a iphone_backup -s iphone_backup_password
```

### 4. Configure Remote SSH Sync (Optional)

Create `~/.whatsapp_export.conf`:

```bash
cat > ~/.whatsapp_export.conf << 'EOF'
SSH_HOST="your.server.com"
SSH_USER="whatsapp_user"
SSH_PATH="/home/whatsapp_user/exports"
VERBOSE=false
EOF
```

**Requirements for SSH:**
- SSH key-based auth (not password)
- Create SSH key if needed:
  ```bash
  ssh-keygen -t ed25519 -f ~/.ssh/id_whatsapp_export
  ```
- Add public key to server:
  ```bash
  ssh-copy-id -i ~/.ssh/id_whatsapp_export.pub whatsapp_user@your.server.com
  ```

Make config file readable only by you:
```bash
chmod 600 ~/.whatsapp_export.conf
```

## Usage

### Run the Pipeline

```bash
cd ~/projects/mikoshi-whatsapp-sync/whatsapp_export

# Incremental sync (default) — only new messages since last run
bash run_pipeline.sh

# Re-pull entire history of one contact (other chats untouched)
bash run_pipeline.sh --mode full-contact --contact "Alice"

# Full resync from scratch (rare; resets .sync_state.json)
bash run_pipeline.sh --mode full

# Run extraction but skip rsync (local-only)
bash run_pipeline.sh --skip-remote-sync

# Show all flags
bash run_pipeline.sh --help
```

**On first run, your iPhone will prompt:**
```
Trust This Computer?
[Don't Allow]  [Allow]
```

Tap **[Allow]**.

### Sync state

After every successful run, `.sync_state.json` is updated:
```json
{
  "version": 1,
  "last_global_sync": "2026-05-25T14:30:15+00:00",
  "chats": {
    "34600123456@s.whatsapp.net": "2026-05-25T14:29:01+00:00",
    "120363012345@g.us": "2026-05-25T14:25:43+00:00"
  }
}
```

Each chat keeps its own cursor, so subsequent runs only pull messages newer than the cursor. `--mode full-contact` only advances the cursor for the targeted JID; everything else stays as-is.

### What Happens

1. **Detection** (~10 seconds)
   - Finds your iPhone on network
   - Verifies USB/WiFi connection

2. **Backup** (~5-10 minutes)
   - Creates temporary encrypted backup
   - Validates backup integrity

3. **Decryption** (~2-3 minutes)
   - Decrypts backup with Keychain password
   - Extracts ChatStorage.sqlite

4. **Export** (~1 minute)
   - Converts SQLite to JSON
   - Includes chats, messages, metadata

5. **Cleanup** (~30 seconds)
   - Securely deletes ChatStorage.sqlite (7-pass shred)
   - Removes temporary backup files
   - Cleans temp/ directory

6. **Remote Sync** (~1-5 minutes, if configured)
   - rsync exports to server via SSH
   - Uses checksums to skip unchanged files

### Output

After successful run:
```
✓ whatsapp_export_20260525_143015.json (2.3 MB)
✓ Remote sync to server@example.com completed
✓ Logs saved to: logs/pipeline_20260525_143015.log
```

## Examples

### Run and monitor progress
```bash
bash run_pipeline.sh
```

### Check logs
```bash
tail -f logs/pipeline_*.log
```

### Verify exports are synced
```bash
ssh whatsapp_user@your.server.com ls -lh /home/whatsapp_user/exports/
```

### Manual cleanup (if needed)
```bash
# Securely delete old exports
find exports/ -mtime +30 -name "*.json" -exec shred -vfz -n 7 {} \;

# Clear logs (keep last 10)
ls -t logs/pipeline_*.log | tail -n +11 | xargs rm -f
```

## Troubleshooting

### "No iPhone detected"

1. Check iPhone is on same WiFi as Mac
2. Unlock iPhone
3. Check trust: iPhone → Settings → General → AirDrop & Handoff → WiFi Sync
4. Try USB connection as fallback:
   ```bash
   idevice_id -l
   ```

### "Backup password not found"

Keychain entry missing. Re-create:
```bash
security add-generic-password \
  -a iphone_backup \
  -s iphone_backup_password \
  -w 'YOUR_BACKUP_PASSWORD'
```

### "rsync connection refused"

Check SSH configuration:
```bash
# Test SSH connection
ssh -i ~/.ssh/id_whatsapp_export whatsapp_user@your.server.com echo "OK"

# Verify config file
cat ~/.whatsapp_export.conf
```

### "Pipeline is already running"

Concurrent execution detected. Either:
1. Wait for existing run to finish
2. Or manually remove lock if stuck:
   ```bash
   rm whatsapp_export/.pipeline.lock
   ```

### ChatStorage.sqlite not found

This means the backup structure is different. Check:
```bash
# List files in temp (before cleanup)
find whatsapp_export/temp -name "*.sqlite"
```

If different location, we may need to update the extraction logic. Please share error logs.

## Security & Privacy

### What stays on disk

- **logs/** - Pipeline execution logs (no passwords)
- **exports/** - JSON files with your conversation data (plain text)

### What's deleted

- Temporary backups (encrypted)
- ChatStorage.sqlite (sensitive SQLite database)
- All decrypted intermediate files
- Uses `shred -vfz -n 7` (7-pass DoD/NIST wipe)

### What never happens

- ❌ Backup doesn't go to iCloud
- ❌ Passwords not in logs or scripts
- ❌ SSH keys not shared or uploaded
- ❌ No third-party services involved

## Advanced Configuration

### Environment Variables

Override defaults via environment:
```bash
export VERBOSE=true
export SSH_HOST="myserver.com"
bash run_pipeline.sh
```

### Disable Remote Sync

Leave `SSH_HOST` empty in `~/.whatsapp_export.conf`:
```bash
# SSH_HOST=""  # Disabled
SSH_USER="whatsapp_user"
SSH_PATH="/path"
```

### Keep Multiple Backup Versions

Add to pipeline if you need historical backups:
```bash
# Inside run_pipeline.sh, before cleanup:
cp "$BACKUP_PATH" "$BACKUPS_DIR/backup_${TIMESTAMP}/"
```

## Logs

Logs saved with full timestamps:
```
logs/
├── setup_20260525_143000.log       # Setup phase
└── pipeline_20260525_143015.log    # Main pipeline
```

Search logs:
```bash
# Recent errors
grep ERROR logs/pipeline_*.log

# Execution timeline
grep "PHASE\|✓" logs/pipeline_*.log | tail -20
```

## Exit Codes

- **0** - Success
- **1** - Device detection or cleanup error
- **2** - Backup/decryption error
- **3** - Remote sync (rsync) error

## Development Notes

### Adding new extraction logic

Edit `export_json()` in `run_pipeline.sh`:
```python
# Modify the PYTHON_EOF section to extract additional data
cursor.execute("SELECT * FROM ZWAMESSAGE WHERE Z_TIMESTAMP > ?", (timestamp,))
```

### Testing without iPhone

For development, mock a backup:
```bash
# Create test ChatStorage.sqlite
sqlite3 temp/ChatStorage.sqlite "CREATE TABLE ZWACHATS (Z_PK INTEGER PRIMARY KEY);"
```

## Schema & server-side processing

Every export conforms to [schema.json](schema.json) (Draft-07, currently v1.1). The pipeline runs `validate_export.py` on the JSON before rsync — a failed validation aborts the run instead of shipping a broken file.

Key fields the Mikoshi server relies on:
- `message.id` — stable PK from `ZWAMESSAGE.Z_PK`. **Used for upsert dedup.** Incremental and full-contact runs produce overlapping records; the server upserts idempotently.
- `chat.jid` — natural key. `is_group` set when `@g.us`, with `participants[]` populated.
- `attachment.sha256` — content-addressed filename inside `attachments/`. Same file across chats stored once.

The receiver is implemented in [`../server/mikoshi_ingestor/`](../server/) — a Python CLI that:
1. Validates each JSON against `schema.json`.
2. Upserts chats/messages/participants into PostgreSQL.
3. Moves attachments from `attachments/` into a permanent bucketed store.
4. Archives processed JSONs into `processed/` (or `quarantine/` on failure).

See [`../server/README.md`](../server/README.md) for full setup.

## Project Context

This is part of **Mikoshi** — a WhatsApp bot + digital twin project. The exported JSON feeds:
- NLP training on conversation patterns
- Retrieval-augmented response generation
- Conversation memory for the digital twin

## License

Personal use - data processing for private ML training.

## Support

For issues:
1. Check logs: `cat logs/pipeline_*.log`
2. Verify setup: `bash setup.sh` again
3. Test iPhone connection: `ideviceinfo -u $(idevice_id -l | head -1)`
4. Check SSH: `ssh -v whatsapp_user@server`

---

**Last updated**: 2026-05-25  
**Pipeline version**: 1.0  
**Tested on**: macOS 12.x, iPhone 14+, libimobiledevice 1.8.x
