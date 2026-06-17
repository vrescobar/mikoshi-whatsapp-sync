import AppKit

/// The preferences window: a tabbed surface (Status · Favorites · Config ·
/// Permissions) built programmatically so there's no nib/storyboard dependency.
final class PrefsWindowController: NSWindowController {
    static let shared = PrefsWindowController()

    private init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 620, height: 480),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Mikoshi WhatsApp Sync"
        window.center()

        let tabs = NSTabViewController()
        tabs.tabStyle = .toolbar
        tabs.addTabViewItem(Self.item(StatusVC(), "Status", "chart.bar"))
        tabs.addTabViewItem(Self.item(FavoritesVC(), "Favorites", "star"))
        tabs.addTabViewItem(Self.item(ConfigVC(), "Config", "gearshape"))
        tabs.addTabViewItem(Self.item(PermissionsVC(), "Permissions", "lock.shield"))
        window.contentViewController = tabs

        super.init(window: window)
    }

    required init?(coder: NSCoder) { fatalError() }

    func show() {
        NSApp.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    private static func item(_ vc: NSViewController, _ label: String,
                             _ symbol: String) -> NSTabViewItem {
        let item = NSTabViewItem(viewController: vc)
        item.label = label
        item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: label)
        return item
    }
}

// MARK: - shared layout helpers

private func vstack(_ views: [NSView], spacing: CGFloat = 8) -> NSStackView {
    let s = NSStackView(views: views)
    s.orientation = .vertical
    s.alignment = .leading
    s.spacing = spacing
    s.translatesAutoresizingMaskIntoConstraints = false
    s.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
    return s
}

private func label(_ text: String, bold: Bool = false) -> NSTextField {
    let l = NSTextField(labelWithString: text)
    if bold { l.font = .boldSystemFont(ofSize: NSFont.systemFontSize) }
    return l
}

// MARK: - Status tab

final class StatusVC: NSViewController {
    private let summary = NSTextField(labelWithString: "")
    private let logView = NSTextView()

    override func loadView() {
        summary.lineBreakMode = .byWordWrapping
        summary.maximumNumberOfLines = 0
        summary.preferredMaxLayoutWidth = 560

        let refresh = NSButton(title: "Refresh", target: self, action: #selector(reload))
        let syncNow = NSButton(title: "Sync now", target: self, action: #selector(syncNow))

        logView.isEditable = false
        logView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.documentView = logView
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.setContentHuggingPriority(.defaultLow, for: .vertical)

        let stack = vstack([
            label("Sync status", bold: true),
            summary,
            NSStackView(views: [syncNow, refresh]),
            label("Latest log", bold: true),
            scroll,
        ])
        scroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true

        let root = NSView()
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        view = root
    }

    override func viewWillAppear() { super.viewWillAppear(); reload() }

    @objc private func reload() {
        let s = StatsModel.current()
        var lines = ["Last successful sync: \(s.lastSyncDisplay)"]
        lines.append("Chats tracked: \(s.chatsTracked)")
        if let st = s.serverTotalMsgs { lines.append("Messages on server: \(st)") }
        if let lm = s.localMaxMsgs { lines.append("Messages locally: \(lm)") }
        if let r = s.iphoneReachable { lines.append("iPhone reachable: \(r ? "yes" : "no")") }
        if let i = s.driftInSync, let la = s.driftLocalAhead, let sa = s.driftServerAhead {
            lines.append("Drift — in sync: \(i), local ahead: \(la), server ahead: \(sa)")
        }
        lines.append(s.isRunning ? "● A sync is running (PID \(s.runningPID ?? 0))"
                                 : "○ Idle")
        if let sched = Schedule.current() {
            let when = sched.frequency == .daily
                ? String(format: "daily at %02d:%02d", sched.hour ?? 0, sched.minute)
                : String(format: "hourly at :%02d", sched.minute)
            lines.append("Schedule: \(when) (\(sched.enabled ? "enabled" : "paused"))")
        } else {
            lines.append("Schedule: not installed")
        }
        summary.stringValue = lines.joined(separator: "\n")
        logView.string = s.lastLogTail ?? "(no log yet)"
        logView.scrollToEndOfDocument(nil)
    }

    @objc private func syncNow() { AppController.shared.syncNow() }
}

// MARK: - Favorites tab

final class FavoritesVC: NSViewController, NSTableViewDataSource, NSTableViewDelegate {
    private var chats: [Chat] = []
    private var selected = Set<String>()
    private let table = NSTableView()
    private let thresholdField = NSTextField(string: "")
    private let status = NSTextField(labelWithString: "")

    override func loadView() {
        table.dataSource = self
        table.delegate = self
        table.usesAlternatingRowBackgroundColors = true
        let cols: [(String, String, CGFloat)] = [
            ("on", "✓", 30), ("name", "Chat", 240),
            ("count", "Messages", 90), ("jid", "JID", 200),
        ]
        for (id, title, w) in cols {
            let c = NSTableColumn(identifier: .init(id))
            c.title = title; c.width = w
            table.addTableColumn(c)
        }
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.documentView = table
        scroll.translatesAutoresizingMaskIntoConstraints = false

        thresholdField.placeholderString = "e.g. 600 (blank = off)"
        thresholdField.widthAnchor.constraint(equalToConstant: 160).isActive = true
        let thresholdRow = NSStackView(views: [
            label("Auto-include DMs with ≥ N messages:"), thresholdField])

        let save = NSButton(title: "Save & apply", target: self, action: #selector(save))
        let reload = NSButton(title: "Reload chats", target: self, action: #selector(reload))

        let stack = vstack([
            label("Pick the chats to sync", bold: true),
            scroll,
            thresholdRow,
            NSStackView(views: [save, reload]),
            status,
        ])
        scroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 280).isActive = true

        let root = NSView()
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor),
            stack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])
        view = root
    }

    override func viewWillAppear() { super.viewWillAppear(); reload() }

    @objc private func reload() {
        let state = FavoritesStore.load()
        selected = state.jids
        thresholdField.stringValue = state.dmMinMessages.map(String.init) ?? ""
        do {
            chats = try ChatDB.listChats()
            status.stringValue = "\(chats.count) chats · \(selected.count) selected"
        } catch {
            chats = []
            status.stringValue = "Cannot read chats: \(error). Check Full Disk Access."
        }
        table.reloadData()
    }

    @objc private func save() {
        let threshold = Int(thresholdField.stringValue.trimmingCharacters(in: .whitespaces))
        FavoritesStore.save(selectedJIDs: selected, dmMinMessages: threshold, chats: chats)
        // Re-bootstrap the agent so the next scheduled run picks up the change
        // (preserving paused state — the wrapper re-reads the file regardless).
        try? Schedule.reapply()
        status.stringValue = "Saved \(selected.count) favorite(s). Applied."
    }

    func numberOfRows(in tableView: NSTableView) -> Int { chats.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?,
                   row: Int) -> NSView? {
        let chat = chats[row]
        switch tableColumn?.identifier.rawValue {
        case "on":
            let cb = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggle(_:)))
            cb.state = selected.contains(chat.jid) ? .on : .off
            cb.tag = row
            return cb
        case "name":
            return NSTextField(labelWithString: chat.isGroup ? "👥 \(chat.name)" : chat.name)
        case "count":
            return NSTextField(labelWithString: "\(chat.msgCount)")
        default:
            return NSTextField(labelWithString: chat.jid)
        }
    }

    @objc private func toggle(_ sender: NSButton) {
        let jid = chats[sender.tag].jid
        if sender.state == .on { selected.insert(jid) } else { selected.remove(jid) }
        status.stringValue = "\(chats.count) chats · \(selected.count) selected"
    }
}

// MARK: - Config tab

final class ConfigVC: NSViewController {
    private var fields: [String: NSTextField] = [:]
    private let passwordField = NSSecureTextField(string: "")
    private let status = NSTextField(labelWithString: "")

    override func loadView() {
        let current = Config.load()
        var rows: [NSView] = [label("Pipeline configuration (~/.mikoshi-ingest.conf)", bold: true)]
        for key in Config.editableKeys {
            let tf: NSTextField = key == "MIKOSHI_TOKEN"
                ? NSSecureTextField(string: current[key] ?? "")
                : NSTextField(string: current[key] ?? "")
            tf.widthAnchor.constraint(equalToConstant: 340).isActive = true
            fields[key] = tf
            let row = NSStackView(views: [labelFixed(key), tf])
            row.orientation = .horizontal
            rows.append(row)
        }
        passwordField.widthAnchor.constraint(equalToConstant: 340).isActive = true
        passwordField.placeholderString = Config.backupPasswordIsSet()
            ? "•••••• (set — leave blank to keep)" : "iPhone backup password"
        rows.append(NSStackView(views: [
            labelFixed("Backup password"), passwordField]))

        rows.append(NSStackView(views: [
            NSButton(title: "Save & apply", target: self, action: #selector(save))]))
        rows.append(status)

        let stack = vstack(rows)
        let root = NSView()
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor),
        ])
        view = root
    }

    private func labelFixed(_ text: String) -> NSTextField {
        let l = label(text)
        l.alignment = .right
        l.widthAnchor.constraint(equalToConstant: 180).isActive = true
        return l
    }

    @objc private func save() {
        var updates: [String: String] = [:]
        for (key, tf) in fields {
            let v = tf.stringValue.trimmingCharacters(in: .whitespaces)
            if !v.isEmpty { updates[key] = v }
        }
        Config.save(updates)
        let pw = passwordField.stringValue
        if !pw.isEmpty { _ = Config.setBackupPassword(pw); passwordField.stringValue = "" }
        try? Schedule.reapply()   // re-bootstrap (preserving paused state)
        status.stringValue = "Saved. Configuration applied."
    }
}

// MARK: - Permissions tab

final class PermissionsVC: NSViewController {
    private let state = NSTextField(labelWithString: "")

    override func loadView() {
        let instructions = """
        Mikoshi reads the Mac WhatsApp database, which macOS protects behind \
        Full Disk Access. Grant it to THIS app only — that keeps the access \
        scoped to Mikoshi instead of to your shell.

        1. Click the button below to open Full Disk Access.
        2. Click +, then add Mikoshi (in ~/Applications).
        3. Turn its switch on.
        4. Quit and reopen Mikoshi from the menu bar.

        Only Mikoshi gains access — your Terminal and other CLI tools do not.
        """
        let body = NSTextField(wrappingLabelWithString: instructions)
        body.preferredMaxLayoutWidth = 560

        let open = NSButton(title: "Open Full Disk Access settings",
                            target: self, action: #selector(openSettings))
        let recheck = NSButton(title: "Re-check", target: self, action: #selector(refresh))

        let stack = vstack([
            label("Full Disk Access", bold: true),
            state,
            NSStackView(views: [open, recheck]),
            body,
        ])
        let root = NSView()
        root.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            stack.topAnchor.constraint(equalTo: root.topAnchor),
        ])
        view = root
    }

    override func viewWillAppear() { super.viewWillAppear(); refresh() }

    @objc private func refresh() {
        if !Permissions.canProbe() {
            state.stringValue = "ℹ︎ WhatsApp for Mac not found — Full Disk Access not required for iPhone-only sync."
            state.textColor = .secondaryLabelColor
        } else if Permissions.hasFullDiskAccess() {
            state.stringValue = "✓ Granted — Mikoshi can read the WhatsApp database."
            state.textColor = .systemGreen
        } else {
            state.stringValue = "🔒 Not granted — add Mikoshi to Full Disk Access below."
            state.textColor = .systemRed
        }
    }

    @objc private func openSettings() { Permissions.openFullDiskAccessSettings() }
}
