import AppKit

/// Detects whether the app has the Full Disk Access it needs, by probing the
/// very file that triggers the TCC prompt. There's no public API to query TCC
/// state, so we test empirically: open the WhatsApp container DB for reading.
enum Permissions {
    /// True when we can actually read the Mac WhatsApp database.
    static func hasFullDiskAccess() -> Bool {
        let path = Paths.macChatStorage.path
        // If WhatsApp isn't installed the file is absent; treat as "no probe
        // possible" → report true so we don't nag about a permission we can't
        // verify and that the iphone_backup-only flow doesn't need.
        guard FileManager.default.fileExists(atPath: path) else { return true }
        guard let fh = FileHandle(forReadingAtPath: path) else { return false }
        defer { try? fh.close() }
        // A successful open of a TCC-protected file is the signal; read a byte
        // to be certain the kernel didn't hand us a deny-on-read descriptor.
        return (try? fh.read(upToCount: 1)) != nil
    }

    /// True only when the probe file exists (WhatsApp desktop installed).
    static func canProbe() -> Bool {
        FileManager.default.fileExists(atPath: Paths.macChatStorage.path)
    }

    /// Deep-link straight to the Full Disk Access list in System Settings.
    static func openFullDiskAccessSettings() {
        let url = URL(string:
            "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!
        NSWorkspace.shared.open(url)
    }
}
