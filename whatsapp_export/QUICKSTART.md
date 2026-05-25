# QuickStart Guide

Get your WhatsApp export pipeline running in 5 minutes.

## Step 1: Prerequisites Check (1 min)

```bash
# Verify you're on macOS
uname -s  # Should output: Darwin

# Verify Homebrew is installed
brew --version

# Verify Python is installed
python3 --version  # Should be 3.8+
```

## Step 2: Enable iPhone WiFi Sync (2 min)

On your **iPhone**:
1. Go to **Settings**
2. Scroll to **General**
3. Tap **AirDrop & Handoff**
4. Toggle **WiFi Sync** to **ON**
5. Make sure your iPhone is on the same WiFi as your Mac

## Step 3: Install Dependencies (2 min)

```bash
cd ~/projects/mikoshi-whatsapp-sync/whatsapp_export
bash setup.sh
```

This will:
- Install libimobiledevice via Homebrew
- Create a Python virtual environment
- Install Python dependencies

## Step 4: Store Backup Password in Keychain (1 min)

When you first sync your iPhone to this Mac, you set an "iPhone Backup Password". 

Find it or create new one, then run:

```bash
security add-generic-password \
  -a iphone_backup \
  -s iphone_backup_password \
  -w 'YOUR_PASSWORD_HERE'
```

**Don't remember the password?**
- Set a new one: iPhone → Settings → [Your Name] → iCloud → iCloud Backup → Choose Backup Options → Backup Password

## Step 5: (Optional) Configure Remote Sync

If you want to sync to a remote server, create `~/.whatsapp_export.conf`:

```bash
cat > ~/.whatsapp_export.conf << 'EOF'
SSH_HOST="your.server.com"
SSH_USER="your_username"
SSH_PATH="/path/to/exports"
EOF

chmod 600 ~/.whatsapp_export.conf
```

**You must have SSH key-based auth set up.** If you don't:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_whatsapp_export
ssh-copy-id -i ~/.ssh/id_whatsapp_export.pub your_username@your.server.com
```

## Step 6: Run the Pipeline!

```bash
cd ~/projects/mikoshi-whatsapp-sync/whatsapp_export
bash run_pipeline.sh
```

On first run, your iPhone will ask "Trust This Computer?" - **Tap Allow**.

The pipeline will:
1. Find your iPhone
2. Create a backup
3. Extract your conversations
4. Save to JSON
5. Securely delete temporary files
6. Sync to server (if configured)

**Time to complete:** 10-20 minutes (mostly waiting for backup)

## Check Results

```bash
# View your export
ls -lh exports/

# View logs
tail -f logs/pipeline_*.log

# If you set up remote sync, verify it arrived:
ssh your_username@your.server.com ls -lh /path/to/exports/
```

## Done! 🎉

Your WhatsApp data is now:
- ✅ Encrypted on disk (in exports/)
- ✅ Synced to your remote server (if configured)
- ✅ Ready for training your Mikoshi AI model

For more details, see [README.md](README.md).

---

**Troubleshooting?** See README.md → Troubleshooting section
