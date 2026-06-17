import Foundation

/// Read-only snapshot of sync state, assembled from the files the pipeline
/// already writes. Nothing here mutates anything.
struct StatsModel {
    var lastSuccessfulCommit: String?   // ISO8601 from .sync_state.json
    var chatsTracked: Int               // count of keys in .sync_state.json "chats"
    var serverTotalMsgs: Int?           // from .tui_cache.json
    var localMaxMsgs: Int?              // from .tui_cache.json
    var iphoneReachable: Bool?          // from .tui_cache.json
    var driftInSync: Int?
    var driftLocalAhead: Int?
    var driftServerAhead: Int?
    var isRunning: Bool                 // .pipeline.lock holds a live PID
    var runningPID: Int32?
    var lastLogTail: String?

    static func current() -> StatsModel {
        var m = StatsModel(
            lastSuccessfulCommit: nil, chatsTracked: 0, serverTotalMsgs: nil,
            localMaxMsgs: nil, iphoneReachable: nil, driftInSync: nil,
            driftLocalAhead: nil, driftServerAhead: nil, isRunning: false,
            runningPID: nil, lastLogTail: nil)

        // .sync_state.json — authoritative, written after every successful push.
        if let obj = readJSON(Paths.syncState) {
            m.lastSuccessfulCommit = obj["last_successful_commit"] as? String
            if let chats = obj["chats"] as? [String: Any] { m.chatsTracked = chats.count }
        }

        // .tui_cache.json — richer snapshot, may be stale if the TUI hasn't run.
        if let obj = readJSON(Paths.tuiCache) {
            m.serverTotalMsgs = obj["server_total_msgs"] as? Int
            m.localMaxMsgs = obj["local_max_msgs"] as? Int
            m.iphoneReachable = obj["iphone_reachable"] as? Bool
            if let drift = obj["drift_summary"] as? [String: Any] {
                m.driftInSync = drift["in_sync"] as? Int
                m.driftLocalAhead = drift["local_ahead"] as? Int
                m.driftServerAhead = drift["server_ahead"] as? Int
            }
        }

        // .pipeline.lock — PID, verified live with kill -0 (signal 0).
        if let pidText = try? String(contentsOf: Paths.lockFile, encoding: .utf8),
           let pid = Int32(pidText.trimmingCharacters(in: .whitespacesAndNewlines)) {
            if kill(pid, 0) == 0 {
                m.isRunning = true
                m.runningPID = pid
            }
        }

        m.lastLogTail = tail(of: Paths.latestCronLog(), lines: 40)
        return m
    }

    /// Human-friendly "last sync" string.
    var lastSyncDisplay: String {
        guard let iso = lastSuccessfulCommit else { return "never" }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let date = parser.date(from: iso)
            ?? ISO8601DateFormatter().date(from: iso)
        guard let date else { return iso }
        let rel = RelativeDateTimeFormatter()
        rel.unitsStyle = .full
        return rel.localizedString(for: date, relativeTo: Date())
    }

    private static func readJSON(_ url: URL) -> [String: Any]? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private static func tail(of url: URL?, lines: Int) -> String? {
        guard let url, let text = try? String(contentsOf: url, encoding: .utf8) else {
            return nil
        }
        // Strip ANSI color codes so the GUI shows clean text.
        let clean = text.replacingOccurrences(
            of: "\u{1B}\\[[0-9;]*m", with: "", options: .regularExpression)
        let all = clean.split(separator: "\n", omittingEmptySubsequences: false)
        return all.suffix(lines).joined(separator: "\n")
    }
}
