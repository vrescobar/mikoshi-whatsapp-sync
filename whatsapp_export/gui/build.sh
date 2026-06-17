#!/usr/bin/env bash
#
# Build Mikoshi.app — a single-binary AppKit menu-bar app, no Xcode project.
#
# Output: gui/build/Mikoshi.app, code-signed with a stable identity so the
# Full Disk Access grant survives rebuilds. Set MIKOSHI_SIGN_ID to the name of
# a self-signed Code Signing certificate (see README); without it we fall back
# to an ad-hoc signature (works, but TCC forgets the grant on every rebuild).
set -euo pipefail

cd "$(dirname "$0")"
HERE="$(pwd)"
BUILD="$HERE/build"
APP="$BUILD/Mikoshi.app"
BIN_NAME="mikoshi-tray"
SIGN_ID="${MIKOSHI_SIGN_ID:-Mikoshi Self-Signed}"
BUNDLE_ID="com.mikoshi.tray"

echo "==> Cleaning"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> Compiling Swift sources"
# Swift 5 language mode keeps strict-concurrency out of the way for this
# small main-thread app. libsqlite3 is linked for the chat browser.
swiftc \
    -swift-version 5 \
    -O \
    -framework AppKit \
    -framework ServiceManagement \
    -lsqlite3 \
    -o "$APP/Contents/MacOS/$BIN_NAME" \
    Sources/*.swift

echo "==> Assembling bundle"
cp Info.plist "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

echo "==> Signing (identity: $SIGN_ID)"
if security find-identity -v -p codesigning 2>/dev/null | grep -qF "$SIGN_ID"; then
    codesign --force --options runtime \
        --identifier "$BUNDLE_ID" \
        --sign "$SIGN_ID" "$APP"
    echo "    signed with '$SIGN_ID' (stable identity — FDA grant persists)"
else
    echo "    WARNING: certificate '$SIGN_ID' not found — using ad-hoc signature."
    echo "    The Full Disk Access grant will reset on every rebuild."
    echo "    Create the cert once (see README) for a stable identity."
    codesign --force --identifier "$BUNDLE_ID" --sign - "$APP"
fi

echo "==> Verifying"
codesign -dv --verbose=2 "$APP" 2>&1 | sed 's/^/    /'
echo "==> Built: $APP"
