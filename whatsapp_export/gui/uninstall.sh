#!/usr/bin/env bash
#
# Remove the app, its launchd agent, and login item. Leaves your config and
# favorites (~/.mikoshi-ingest.conf, ~/.mikoshi-favorites.json) untouched.
set -euo pipefail

BUNDLE_ID="com.mikoshi.tray"
APP="$HOME/Applications/mikoshi-sync.app"
LEGACY_APP="$HOME/Applications/Mikoshi.app"   # pre-rename bundle name
LABEL="com.mikoshi.sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Quitting the app"
osascript -e 'tell application "mikoshi-sync" to quit' 2>/dev/null || true
pkill -f "Contents/MacOS/mikoshi-tray" 2>/dev/null || true

echo "==> Removing launchd agent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "==> Clearing recorded repo path"
defaults delete "$BUNDLE_ID" 2>/dev/null || true

echo "==> Deleting the app"
rm -rf "$APP" "$LEGACY_APP"

cat <<EOF

Uninstalled. Note:
  - Remove the stale 'mikoshi-sync' entry from System Settings ▸ Privacy & Security
    ▸ Full Disk Access manually (macOS keeps it listed).
  - Your config and favorites were left in place:
      ~/.mikoshi-ingest.conf
      ~/.mikoshi-favorites.json
EOF
