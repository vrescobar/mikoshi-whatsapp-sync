import Foundation

/// Central path resolution for the GUI.
///
/// The app bundle lives in ~/Applications and can't infer where the
/// `whatsapp_export` repo sits, so `install.sh` records it with:
///
///     defaults write com.mikoshi.tray RepoDir "<…>/whatsapp_export"
///
/// We read that here. The fallback is the install location on this Mac so a
/// freshly-built (not-yet-installed) bundle still works during development.
enum Paths {
    /// Dev-only fallback when `RepoDir` hasn't been recorded (e.g. running the
    /// built bundle without install.sh). Derived from the current home dir so
    /// no username is baked into the source. Override via the env var
    /// MIKOSHI_REPO_DIR if your checkout lives elsewhere.
    static var fallbackRepoDir: String {
        if let env = ProcessInfo.processInfo.environment["MIKOSHI_REPO_DIR"], !env.isEmpty {
            return env
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("projects/mikoshi-whatsapp-sync/whatsapp_export")
            .path
    }

    /// The `whatsapp_export` directory (a.k.a. SCRIPT_DIR in the shell tools).
    static var repoDir: URL {
        let stored = UserDefaults.standard.string(forKey: "RepoDir")
        let path = (stored?.isEmpty == false ? stored! : fallbackRepoDir)
        return URL(fileURLWithPath: path, isDirectory: true)
    }

    static var wrapper: URL { repoDir.appendingPathComponent("mikoshi-whatsapp.sh") }
    static var syncState: URL { repoDir.appendingPathComponent(".sync_state.json") }
    static var tuiCache: URL { repoDir.appendingPathComponent(".tui_cache.json") }
    static var lockFile: URL { repoDir.appendingPathComponent(".pipeline.lock") }
    static var logsDir: URL { repoDir.appendingPathComponent("logs") }

    static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    static var ingestConf: URL { home.appendingPathComponent(".mikoshi-ingest.conf") }

    /// Favorites file — honours MIKOSHI_FAVORITES_FILE from the conf, else the
    /// default ~/.mikoshi-favorites.json (matches favorites.py:path()).
    static var favoritesFile: URL {
        if let override = Config.load()["MIKOSHI_FAVORITES_FILE"], !override.isEmpty {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return home.appendingPathComponent(".mikoshi-favorites.json")
    }

    /// The live Mac WhatsApp database — the file whose access triggers the
    /// "access data from other apps" TCC prompt, and our FDA litmus test.
    static var macChatStorage: URL {
        home
            .appendingPathComponent("Library/Group Containers")
            .appendingPathComponent("group.net.whatsapp.WhatsApp.shared")
            .appendingPathComponent("ChatStorage.sqlite")
    }

    /// Absolute path to this running binary (used for the launchd --sync-now plist).
    static var executable: URL {
        URL(fileURLWithPath: Bundle.main.executablePath ?? CommandLine.arguments[0])
    }

    /// Latest logs/cron_*.log, if any.
    static func latestCronLog() -> URL? {
        let fm = FileManager.default
        guard let entries = try? fm.contentsOfDirectory(
            at: logsDir, includingPropertiesForKeys: [.contentModificationDateKey]
        ) else { return nil }
        return entries
            .filter { $0.lastPathComponent.hasPrefix("cron_") && $0.pathExtension == "log" }
            .sorted { a, b in a.lastPathComponent < b.lastPathComponent }
            .last
    }
}
