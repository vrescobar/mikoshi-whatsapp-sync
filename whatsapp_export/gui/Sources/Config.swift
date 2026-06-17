import Foundation

/// Read/write ~/.mikoshi-ingest.conf, a bash-sourceable KEY=VALUE file.
///
/// We deliberately keep this dumb: parse `KEY=VALUE` (stripping surrounding
/// quotes), and rewrite the whole file as `KEY="VALUE"`. Comments and unknown
/// keys are preserved on a best-effort basis by round-tripping them.
enum Config {
    /// Keys the GUI surfaces in the Config tab.
    static let editableKeys = [
        "MIKOSHI_URL",
        "MIKOSHI_TOKEN",
        "MIKOSHI_BACKUP_DIR",
        "MIKOSHI_SOURCES",
        "MIKOSHI_FAVORITES_FILE",
        "MIKOSHI_EXTRACT_TIMEOUT",
        "MIKOSHI_SECURE_CLEANUP",
        "KEEP_LOCAL_EXPORTS",
    ]

    static func load() -> [String: String] {
        guard let text = try? String(contentsOf: Paths.ingestConf, encoding: .utf8) else {
            return [:]
        }
        var out: [String: String] = [:]
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            let stripped = line.hasPrefix("export ")
                ? String(line.dropFirst("export ".count)) : line
            guard let eq = stripped.firstIndex(of: "=") else { continue }
            let key = String(stripped[..<eq]).trimmingCharacters(in: .whitespaces)
            var val = String(stripped[stripped.index(after: eq)...])
                .trimmingCharacters(in: .whitespaces)
            val = unquote(val)
            out[key] = val
        }
        return out
    }

    /// Write `updates` over the existing conf, preserving comment lines and
    /// any keys we don't manage. Always chmod 0600 (it can hold the token).
    static func save(_ updates: [String: String]) {
        var lines: [String] = []
        var seen = Set<String>()
        let existing = (try? String(contentsOf: Paths.ingestConf, encoding: .utf8)) ?? ""
        for raw in existing.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let body = trimmed.hasPrefix("export ")
                ? String(trimmed.dropFirst("export ".count)) : trimmed
            if let eq = body.firstIndex(of: "="), !trimmed.hasPrefix("#") {
                let key = String(body[..<eq]).trimmingCharacters(in: .whitespaces)
                if let newVal = updates[key] {
                    lines.append("\(key)=\(quote(newVal))")
                    seen.insert(key)
                    continue
                }
            }
            lines.append(line)
        }
        // Append keys that weren't already present.
        for (key, val) in updates where !seen.contains(key) {
            lines.append("\(key)=\(quote(val))")
        }
        let output = lines.joined(separator: "\n")
        try? output.write(to: Paths.ingestConf, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: Paths.ingestConf.path)
    }

    // MARK: - Keychain (iPhone backup password)

    static func backupPasswordIsSet() -> Bool {
        let r = Shell.run("/usr/bin/security",
            ["find-generic-password", "-a", "iphone_backup",
             "-s", "iphone_backup_password", "-w"])
        return r.status == 0 && !r.stdout.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    static func setBackupPassword(_ pw: String) -> Bool {
        // -U updates if the item already exists.
        let r = Shell.run("/usr/bin/security",
            ["add-generic-password", "-U", "-a", "iphone_backup",
             "-s", "iphone_backup_password", "-w", pw])
        return r.status == 0
    }

    // MARK: - helpers

    private static func unquote(_ s: String) -> String {
        if s.count >= 2,
           (s.hasPrefix("\"") && s.hasSuffix("\"")) || (s.hasPrefix("'") && s.hasSuffix("'")) {
            return String(s.dropFirst().dropLast())
        }
        return s
    }

    private static func quote(_ s: String) -> String {
        "\"\(s.replacingOccurrences(of: "\"", with: "\\\""))\""
    }
}
