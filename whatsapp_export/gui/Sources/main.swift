import AppKit

// Headless path: launchd fires `mikoshi-tray --sync-now`. Run one sync under
// this signed binary's identity (so it inherits Full Disk Access) and exit
// with the wrapper's status code — no UI, no event loop.
if CommandLine.arguments.contains("--sync-now") {
    let extra = Array(CommandLine.arguments.dropFirst()).filter { $0 != "--sync-now" }
    exit(SyncRunner.runHeadless(extraArgs: extra))
}

// GUI path: a menu-bar-only agent (LSUIElement) — no Dock icon, no main menu.
let app = NSApplication.shared
let controller = AppController.shared
app.delegate = controller
app.setActivationPolicy(.accessory)
app.run()
