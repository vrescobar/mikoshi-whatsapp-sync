import Foundation
import ServiceManagement

/// Manages the launchd agent that fires scheduled syncs, and the login item
/// that keeps the menu-bar app resident.
///
/// Reuses the existing label `com.mikoshi.sync` (so it transparently replaces
/// the bash-based agent that scheduler.py installs) but repoints
/// ProgramArguments at THIS signed app binary with `--sync-now`. That makes the
/// app the single TCC identity for both manual and scheduled syncs.
enum Schedule {
    static let label = "com.mikoshi.sync"
    static var plistURL: URL {
        Paths.home.appendingPathComponent("Library/LaunchAgents/\(label).plist")
    }

    enum Frequency: String { case daily, hourly }

    struct Info {
        var enabled: Bool
        var hour: Int?      // nil ⇒ hourly
        var minute: Int
        var frequency: Frequency
    }

    // MARK: - read

    static func current() -> Info? {
        guard let data = try? Data(contentsOf: plistURL),
              let plist = try? PropertyListSerialization.propertyList(
                from: data, format: nil) as? [String: Any]
        else { return nil }
        let interval = plist["StartCalendarInterval"] as? [String: Any] ?? [:]
        let disabled = (plist["Disabled"] as? Bool) ?? false
        let minute = (interval["Minute"] as? Int) ?? 0
        if let hour = interval["Hour"] as? Int {
            return Info(enabled: !disabled, hour: hour, minute: minute, frequency: .daily)
        }
        return Info(enabled: !disabled, hour: nil, minute: minute, frequency: .hourly)
    }

    // MARK: - write

    /// Install/replace the agent. `hour` is required for daily, ignored for hourly.
    static func install(frequency: Frequency, hour: Int, minute: Int,
                        enabled: Bool = true) throws {
        var interval: [String: Any] = ["Minute": minute]
        if frequency == .daily { interval["Hour"] = hour }

        let plist: [String: Any] = [
            "Label": label,
            "ProgramArguments": [Paths.executable.path, "--sync-now"],
            "StartCalendarInterval": interval,
            "StandardOutPath": Paths.logsDir.appendingPathComponent("launchagent.out.log").path,
            "StandardErrorPath": Paths.logsDir.appendingPathComponent("launchagent.err.log").path,
            "RunAtLoad": false,
            "Disabled": !enabled,
        ]
        try writeAndBoot(plist, enabled: enabled)
    }

    static func pause() throws {
        guard var info = current() else { return }
        info.enabled = false
        try install(frequency: info.frequency, hour: info.hour ?? 0,
                    minute: info.minute, enabled: false)
        bootout()
    }

    static func resume() throws {
        guard let info = current() else { return }
        try install(frequency: info.frequency, hour: info.hour ?? 0,
                    minute: info.minute, enabled: true)
    }

    /// Re-bootstrap the agent preserving its current enabled/paused state.
    /// Used after a config/favorites change so a scheduled run sees it — though
    /// the wrapper also re-reads those files on every run, so this is belt-and-
    /// braces and must NOT silently un-pause a paused schedule.
    static func reapply() throws {
        guard let info = current() else { return }   // nothing installed → no-op
        try install(frequency: info.frequency, hour: info.hour ?? 0,
                    minute: info.minute, enabled: info.enabled)
    }

    private static func writeAndBoot(_ plist: [String: Any], enabled: Bool) throws {
        try FileManager.default.createDirectory(
            at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: Paths.logsDir, withIntermediateDirectories: true)
        let data = try PropertyListSerialization.data(
            fromPropertyList: plist, format: .xml, options: 0)
        let tmp = plistURL.appendingPathExtension("tmp")
        try data.write(to: tmp)
        _ = try? FileManager.default.replaceItemAt(plistURL, withItemAt: tmp)
        bootout()
        if enabled { bootstrap() }
    }

    // MARK: - launchctl

    private static var domain: String { "gui/\(getuid())" }

    private static func bootstrap() {
        Shell.run("/bin/launchctl", ["bootstrap", domain, plistURL.path])
    }

    @discardableResult
    private static func bootout() -> Bool {
        Shell.run("/bin/launchctl", ["bootout", "\(domain)/\(label)"]).status == 0
    }

    // MARK: - login item (resident menu-bar app)

    static func loginItemEnabled() -> Bool {
        SMAppService.mainApp.status == .enabled
    }

    static func setLoginItem(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
        } catch {
            NSLog("Mikoshi: login item toggle failed: \(error)")
        }
    }
}
