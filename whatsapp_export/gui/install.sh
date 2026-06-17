#!/usr/bin/env bash
#
# Build, install to ~/Applications, record the repo path, migrate the launchd
# agent to the app binary, register the login item, and open Full Disk Access.
set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"
REPO_DIR="$(cd .. && pwd)"          # the whatsapp_export directory
BUNDLE_ID="com.mikoshi.tray"
DEST="$HOME/Applications"
APP="$DEST/Mikoshi.app"
LABEL="com.mikoshi.sync"

echo "==> Building"
./build.sh

echo "==> Installing to $DEST"
mkdir -p "$DEST"
rm -rf "$APP"
cp -R "$HERE/build/Mikoshi.app" "$APP"

echo "==> Recording repo path for the app"
# The bundle can't infer where the repo lives; record it (read by Paths.swift).
defaults write "$BUNDLE_ID" RepoDir "$REPO_DIR"

echo "==> Migrating the launchd agent to the signed app"
# Replace any existing bash-based com.mikoshi.sync with one that runs the app
# binary (single TCC identity). We bootout the old one; the app re-creates the
# plist with the correct schedule the first time you set it from the GUI, or we
# seed a sensible default here if none exists.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ -f "$PLIST" ]; then
    echo "    existing schedule found — repointing it at the app binary"
    /usr/bin/python3 - "$PLIST" "$APP/Contents/MacOS/mikoshi-tray" <<'PY'
import plistlib, sys
plist_path, binary = sys.argv[1], sys.argv[2]
with open(plist_path, "rb") as f:
    data = plistlib.load(f)
data["ProgramArguments"] = [binary, "--sync-now"]
with open(plist_path, "wb") as f:
    plistlib.dump(data, f)
PY
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
else
    echo "    no schedule installed yet — set one from the app's Status/Config tab"
fi

echo "==> Launching the app (registers the login item on first run)"
open "$APP"

echo "==> Opening Full Disk Access settings"
echo "    Add 'Mikoshi' (in ~/Applications), enable it, then relaunch from the menu bar."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" || true

cat <<EOF

Done. Next steps:
  1. In the Full Disk Access list, click + and add: $APP
  2. Toggle it on.
  3. Quit Mikoshi from the menu bar and reopen it (or run 'open $APP').
  4. Use the menu-bar icon → "Sync now" to verify no TCC prompt appears.
EOF
