import AppKit

/// Owns the menu-bar item, the refresh timer, and sync orchestration.
final class AppController: NSObject, NSApplicationDelegate {
    static let shared = AppController()

    private var statusItem: NSStatusItem!
    private var timer: Timer?
    private let lastSyncMenuItem = NSMenuItem(title: "Last sync: …", action: nil, keyEquivalent: "")
    private let runStateMenuItem = NSMenuItem(title: "Idle", action: nil, keyEquivalent: "")
    private let pauseResumeItem = NSMenuItem(title: "Pause schedule",
                                             action: #selector(togglePause), keyEquivalent: "")

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        buildMenu()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    // MARK: - menu

    private func buildMenu() {
        let menu = NSMenu()
        menu.addItem(lastSyncMenuItem)
        menu.addItem(runStateMenuItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Sync now", action: #selector(syncNowMenu), keyEquivalent: "s"))
        menu.addItem(NSMenuItem(title: "Dry run (no push)", action: #selector(dryRun), keyEquivalent: ""))
        menu.addItem(pauseResumeItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Open Mikoshi…", action: #selector(openPrefs), keyEquivalent: ","))
        menu.addItem(NSMenuItem(title: "View last log", action: #selector(viewLog), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        for item in menu.items where item.action != nil && item.target == nil {
            if item.action != #selector(NSApplication.terminate(_:)) { item.target = self }
        }
        statusItem.menu = menu
    }

    // MARK: - state refresh

    private func refresh() {
        let s = StatsModel.current()
        let fdaMissing = Permissions.canProbe() && !Permissions.hasFullDiskAccess()

        let symbol: String
        if SyncRunner.shared.isRunning || s.isRunning {
            symbol = "arrow.triangle.2.circlepath"
        } else if fdaMissing {
            symbol = "lock.fill"
        } else {
            symbol = "checkmark.seal"
        }
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Mikoshi")
            button.image?.isTemplate = true
        }

        lastSyncMenuItem.title = "Last sync: \(s.lastSyncDisplay)"
        if fdaMissing {
            runStateMenuItem.title = "⚠︎ Full Disk Access needed"
        } else if SyncRunner.shared.isRunning || s.isRunning {
            runStateMenuItem.title = "● Syncing…"
        } else if let total = s.serverTotalMsgs {
            runStateMenuItem.title = "Idle · \(total) messages on server"
        } else {
            runStateMenuItem.title = "Idle"
        }

        if let sched = Schedule.current() {
            pauseResumeItem.title = sched.enabled ? "Pause schedule" : "Resume schedule"
            pauseResumeItem.isEnabled = true
        } else {
            pauseResumeItem.title = "Schedule not installed"
            pauseResumeItem.isEnabled = false
        }
    }

    // MARK: - actions

    @objc private func syncNowMenu() { syncNow() }

    func syncNow(extraArgs: [String] = []) {
        guard !SyncRunner.shared.isRunning else { return }
        SyncRunner.shared.start(extraArgs: extraArgs, onLine: { _ in }, onExit: { [weak self] _ in
            self?.refresh()
        })
        refresh()
    }

    @objc private func dryRun() { syncNow(extraArgs: ["--skip-remote-sync"]) }

    @objc private func togglePause() {
        guard let sched = Schedule.current() else { return }
        do {
            if sched.enabled { try Schedule.pause() } else { try Schedule.resume() }
        } catch {
            NSLog("Mikoshi: pause/resume failed: \(error)")
        }
        refresh()
    }

    @objc private func openPrefs() { PrefsWindowController.shared.show() }

    @objc private func viewLog() {
        if let log = Paths.latestCronLog() {
            NSWorkspace.shared.open(log)
        }
    }
}
