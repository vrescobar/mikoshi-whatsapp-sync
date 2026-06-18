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
        window.title = "mikoshi-sync"
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

final class FavoritesVC: NSViewController, NSTableViewDataSource, NSTableViewDelegate,
                         NSTextFieldDelegate, NSSearchFieldDelegate {
    private enum SortKey: String { case name, count, jid, selected }

    /// Every chat read from the DB. `chats` is the filtered + sorted view that
    /// the table actually renders; selection is tracked by JID so it survives
    /// any reorder or filter.
    private var allChats: [Chat] = []
    private var chats: [Chat] = []
    private var selected = Set<String>()
    private var sortKey: SortKey = .count
    private var sortAscending = false

    private let table = NSTableView()
    private let searchField = NSSearchField()
    private let thresholdField = NSTextField(string: "")
    private let thresholdPreview = NSTextField(labelWithString: "")
    private let status = NSTextField(labelWithString: "")

    override func loadView() {
        table.dataSource = self
        table.delegate = self
        table.usesAlternatingRowBackgroundColors = true
        table.allowsColumnSelection = false
        // (id, title, width, sort key). Clicking a header sorts by that key and
        // toggles direction — "Messages" descending shows the chattiest first.
        let cols: [(String, String, CGFloat, SortKey)] = [
            ("on", "✓", 30, .selected), ("name", "Chat", 240, .name),
            ("count", "Messages", 90, .count), ("jid", "JID", 200, .jid),
        ]
        for (id, title, w, key) in cols {
            let c = NSTableColumn(identifier: .init(id))
            c.title = title; c.width = w
            c.sortDescriptorPrototype = NSSortDescriptor(key: key.rawValue, ascending: true)
            table.addTableColumn(c)
        }
        table.sortDescriptors = [NSSortDescriptor(key: SortKey.count.rawValue, ascending: false)]
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.documentView = table
        scroll.translatesAutoresizingMaskIntoConstraints = false

        searchField.placeholderString = "Search name or number (fuzzy)"
        searchField.delegate = self
        searchField.sendsWholeSearchString = false
        searchField.sendsSearchStringImmediately = true

        thresholdField.placeholderString = "e.g. 100 (blank = off)"
        thresholdField.widthAnchor.constraint(equalToConstant: 110).isActive = true
        thresholdField.delegate = self
        thresholdPreview.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        thresholdPreview.textColor = .secondaryLabelColor
        let thresholdRow = NSStackView(views: [
            label("Auto-include DMs with ≥ N messages:"), thresholdField, thresholdPreview])

        let save = NSButton(title: "Save & apply", target: self, action: #selector(save))
        let reload = NSButton(title: "Reload chats", target: self, action: #selector(reload))

        let stack = vstack([
            label("Pick the chats to sync", bold: true),
            searchField,
            scroll,
            thresholdRow,
            NSStackView(views: [save, reload]),
            status,
        ])
        searchField.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32).isActive = true
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
            allChats = try ChatDB.listChats()
            applyView()
        } catch {
            allChats = []
            chats = []
            table.reloadData()
            status.stringValue = "Cannot read chats: \(error). Check Full Disk Access."
        }
        updateThresholdPreview()
    }

    // MARK: search + sort

    /// Filter `allChats` by the (fuzzy) search term, sort by the active column,
    /// and refresh the table. Selection is by JID, so it is unaffected.
    private func applyView() {
        let needle = searchField.stringValue.trimmingCharacters(in: .whitespaces)
        let filtered = needle.isEmpty ? allChats : allChats.filter {
            Fuzzy.matches(needle, $0.name) || Fuzzy.matches(needle, $0.jid)
        }
        chats = filtered.sorted(by: ordersBefore)
        table.reloadData()
        updateStatus()
    }

    private func ordersBefore(_ a: Chat, _ b: Chat) -> Bool {
        let asc = sortAscending
        switch sortKey {
        case .name:
            let r = a.name.folded.compare(b.name.folded)
            if r != .orderedSame { return asc == (r == .orderedAscending) }
            return a.msgCount > b.msgCount
        case .jid:
            if a.jid != b.jid { return asc == (a.jid < b.jid) }
            return a.msgCount > b.msgCount
        case .count:
            if a.msgCount != b.msgCount { return asc == (a.msgCount < b.msgCount) }
            return a.name.folded < b.name.folded
        case .selected:
            let sa = selected.contains(a.jid), sb = selected.contains(b.jid)
            if sa != sb { return asc == (!sa && sb) }
            return a.msgCount > b.msgCount   // tie-break: chattiest first
        }
    }

    func tableView(_ tableView: NSTableView,
                   sortDescriptorsDidChange oldDescriptors: [NSSortDescriptor]) {
        guard let d = tableView.sortDescriptors.first,
              let key = d.key.flatMap(SortKey.init(rawValue:)) else { return }
        sortKey = key
        sortAscending = d.ascending
        applyView()
    }

    private func updateStatus() {
        let shown = chats.count, total = allChats.count
        let scope = shown == total ? "\(total) chats" : "\(shown) of \(total) chats"
        status.stringValue = "\(scope) · \(selected.count) selected"
    }

    /// Parse the threshold field. Returns `.some(nil)` when blank (rule off),
    /// `.some(.some(n))` for a valid n ≥ 1, and `nil` when the text is invalid.
    private func parsedThreshold() -> Int?? {
        let raw = thresholdField.stringValue.trimmingCharacters(in: .whitespaces)
        if raw.isEmpty { return .some(nil) }
        if let t = Int(raw), t >= 1 { return .some(.some(t)) }
        return nil
    }

    /// Live feedback: how many DMs the current threshold would auto-include.
    private func updateThresholdPreview() {
        guard let value = parsedThreshold() else {
            thresholdPreview.stringValue = "⚠︎ whole number ≥ 1"
            thresholdPreview.textColor = .systemRed
            return
        }
        thresholdPreview.textColor = .secondaryLabelColor
        guard let t = value else {
            thresholdPreview.stringValue = allChats.isEmpty ? "" : "rule off"
            return
        }
        // Count over the full set, not the current search filter.
        let n = allChats.filter { !$0.isGroup && $0.msgCount >= t }.count
        thresholdPreview.stringValue = "→ \(n) DM\(n == 1 ? "" : "s") auto-synced"
    }

    func controlTextDidChange(_ obj: Notification) {
        switch obj.object as? NSTextField {
        case thresholdField: updateThresholdPreview()
        case searchField: applyView()
        default: break
        }
    }

    @objc private func save() {
        guard let threshold = parsedThreshold() else {
            status.stringValue = "⚠︎ Threshold must be a whole number ≥ 1 (or blank to turn it off)."
            return
        }
        FavoritesStore.save(selectedJIDs: selected, dmMinMessages: threshold, chats: chats)
        // Re-bootstrap the agent so the next scheduled run picks up the change
        // (preserving paused state — the wrapper re-reads the file regardless).
        try? Schedule.reapply()
        let rule = threshold.map { " · auto-syncing DMs ≥ \($0)" } ?? ""
        status.stringValue = "Saved \(selected.count) favorite(s)\(rule). Applied."
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
        guard chats.indices.contains(sender.tag) else { return }
        let jid = chats[sender.tag].jid
        if sender.state == .on { selected.insert(jid) } else { selected.remove(jid) }
        updateStatus()
    }
}

// MARK: - fuzzy matching

private extension String {
    /// Case- and diacritic-insensitive form for forgiving comparisons.
    var folded: String {
        folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
    }
}

private enum Fuzzy {
    /// True when every character of `needle` appears, in order (not necessarily
    /// adjacent), in `hay` — the classic subsequence fuzzy match. Folding makes
    /// it case/accent-insensitive, so "jose" matches "José" and "vctr" matches
    /// "Víctor". Membership only; ordering is handled by the column sort.
    static func matches(_ needle: String, _ hay: String) -> Bool {
        let n = Array(needle.folded), h = Array(hay.folded)
        if n.isEmpty { return true }
        var i = 0
        for ch in h where ch == n[i] {
            i += 1
            if i == n.count { return true }
        }
        return false
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
        mikoshi-sync reads the Mac WhatsApp database, which macOS protects behind \
        Full Disk Access. Grant it to THIS app only — that keeps the access \
        scoped to mikoshi-sync instead of to your shell.

        1. Click the button below to open Full Disk Access.
        2. Click +, then add mikoshi-sync (in ~/Applications).
        3. Turn its switch on.
        4. Quit and reopen mikoshi-sync from the menu bar.

        Only mikoshi-sync gains access — your Terminal and other CLI tools do not.
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
            state.stringValue = "✓ Granted — mikoshi-sync can read the WhatsApp database."
            state.textColor = .systemGreen
        } else {
            state.stringValue = "🔒 Not granted — add mikoshi-sync to Full Disk Access below."
            state.textColor = .systemRed
        }
    }

    @objc private func openSettings() { Permissions.openFullDiskAccessSettings() }
}
