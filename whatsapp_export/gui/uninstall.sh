#!/usr/bin/env bash
#
# Remove the app, its launchd agent, and login item. Leaves your config and
# favorites (~/.mikoshi-ingest.conf, ~/.mikoshi-favorites.json) untouched.
set -euo pipefail

BUNDLE_ID="com.mikoshi.tray"
APP="$HOME/Applications/Mikoshi.app"
LABEL="com.mikoshi.sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "==> Quitting the app"
osascript -e 'tell application "Mikoshi" to quit' 2>/dev/null || true
pkill -f "Mikoshi.app/Contents/MacOS/mikoshi-tray" 2>/dev/null || true

echo "==> Removing launchd agent"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "==> Clearing recorded repo path"
defaults delete "$BUNDLE_ID" 2>/dev/null || true

echo "==> Deleting the app"
rm -rf "$APP"

cat <<EOF

Uninstalled. Note:
  - Remove the stale 'Mikoshi' entry from System Settings ▸ Privacy & Security
    ▸ Full Disk Access manually (macOS keeps it listed).
  - Your config and favorites were left in place:
      ~/.mikoshi-ingest.conf
      ~/.mikoshi-favorites.json
EOF
