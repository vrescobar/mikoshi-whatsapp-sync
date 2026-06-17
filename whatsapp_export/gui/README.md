# Mikoshi menu-bar app

A tiny native macOS menu-bar app that drives the WhatsApp sync pipeline and —
the whole point — **scopes Full Disk Access to one signed app instead of to your
shell**.

## Why this exists

The sync reads the live Mac WhatsApp database at
`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`.
macOS guards that path behind TCC, producing the recurring prompt:

> *"gtimeout" would like to access data from other apps.*

The tempting fix — granting Full Disk Access (FDA) to `/bin/bash` — is **too
broad**: every script you ever run would silently inherit that access. TCC keys
grants to a **code-signed bundle identity**, not a path, so the only way to scope
FDA to exactly one process is a signed `.app`. This app is that bundle. Grant FDA
to `Mikoshi.app` alone and:

- Its child processes (`mikoshi-whatsapp.sh` → `python3` → `gtimeout`) inherit the
  grant — no prompt, including for the WhatsApp container.
- Your interactive Terminal stays unprivileged (its responsible process is
  Terminal.app, a different identity).

Scheduled syncs run under the **same** identity: the `com.mikoshi.sync` launchd
agent is repointed from `/bin/bash … sync` to
`Mikoshi.app/Contents/MacOS/mikoshi-tray --sync-now`, so one FDA grant covers both
manual and automatic runs while keeping launchd's wake-from-sleep behaviour.

## Dependencies

None beyond the OS: Swift/AppKit/ServiceManagement and system `libsqlite3`, built
with `swiftc` from the **Xcode Command Line Tools** (`xcode-select --install`).
No Xcode project, no third-party packages.

## One-time: create a stable signing certificate

Sign with a self-signed **Code Signing** certificate so the FDA grant survives
rebuilds. (An ad-hoc signature works too, but its hash changes every build, so
macOS forgets the grant and re-prompts each time.)

1. Open **Keychain Access** → menu **Certificate Assistant ▸ Create a
   Certificate…**
2. Name: `Mikoshi Self-Signed` · Identity Type: **Self Signed Root** · Certificate
   Type: **Code Signing** → Create.

`build.sh` picks it up automatically (override the name with `MIKOSHI_SIGN_ID`).

## Build & install

```bash
cd whatsapp_export/gui
./install.sh          # builds, copies to ~/Applications, migrates launchd,
                      # launches the app, opens the FDA settings pane
```

Then, in the Full Disk Access list that opens:

1. Click **+** and add `~/Applications/Mikoshi.app`.
2. Toggle it **on**.
3. Quit Mikoshi from the menu bar and reopen it (`open ~/Applications/Mikoshi.app`).

Verify with the menu-bar icon → **Sync now**: it should run with no TCC prompt.

To build without installing: `./build.sh` → `gui/build/Mikoshi.app`.

## What the app does

Menu-bar icon (idle ✓ / syncing ⟳ / FDA-missing 🔒) with: last-sync time,
messages-on-server, **Sync now**, **Dry run**, **Pause/Resume schedule**, **Open
Mikoshi…**, **View last log**.

The window (**Open Mikoshi…**) has four tabs:

| Tab | Does |
|---|---|
| **Status** | Last commit, drift, iPhone reachability, next scheduled fire, latest log tail. |
| **Favorites** | Browses `ChatStorage.sqlite` (name · JID · message count), tick chats to sync, set the `dm_min_messages` threshold. Writes `~/.mikoshi-favorites.json` with the same group-never-prune rule as `favorites.py`, then re-bootstraps the agent. |
| **Config** | Edits `~/.mikoshi-ingest.conf` (server URL/token, backup dir, sources, timeouts) and the Keychain backup password. Saved `0600`. |
| **Permissions** | Detects FDA by probing the WhatsApp DB; deep-links to the settings pane with step-by-step instructions. |

## How it integrates (reuses existing tooling)

- Sync: spawns `mikoshi-whatsapp.sh sync` (and `--skip-remote-sync` for dry runs).
- Stats: reads `.sync_state.json`, `.tui_cache.json`, latest `logs/cron_*.log`,
  and `.pipeline.lock` (PID liveness).
- Favorites: same JSON format/semantics as `favorites.py`.
- The repo path is recorded at install time via
  `defaults write com.mikoshi.tray RepoDir "<…>/whatsapp_export"`.

## Caveat: scheduling and the TUI

The app and the terminal **TUI** both manage the `com.mikoshi.sync` agent. If you
(re)set the schedule from the TUI (`tui.py` → `scheduler.py`), it rewrites the
plist to the **bash** form, which won't have FDA for scheduled `mac_live` runs.
After using the TUI's schedule menu, re-apply from the app (Config tab → Save, or
Pause then Resume) to repoint the agent back at the signed binary. Simplest rule:
**manage the schedule from the app once installed.**

## Uninstall

```bash
./uninstall.sh        # removes app, launchd agent, login item
```

Leaves `~/.mikoshi-ingest.conf` and `~/.mikoshi-favorites.json` intact. Remove the
stale `Mikoshi` row from the Full Disk Access list manually (macOS keeps it).
