import Foundation

/// Spawns `mikoshi-whatsapp.sh sync` as a child process. Because this app holds
/// the Full Disk Access grant, the child (and its python3/gtimeout descendants)
/// inherit it — no TCC prompt, including for the mac_live WhatsApp container.
///
/// We launch through `/bin/bash -lc` so the wrapper gets a login environment
/// (PATH + Keychain access), exactly as the original launchd agent did.
final class SyncRunner {
    static let shared = SyncRunner()

    private(set) var isRunning = false
    private var process: Process?

    /// Run a sync. `extraArgs` lets callers add e.g. `--skip-remote-sync`.
    /// `onLine` streams cleaned output; `onExit` fires with the status code.
    func start(extraArgs: [String] = [],
               onLine: ((String) -> Void)? = nil,
               onExit: ((Int32) -> Void)? = nil) {
        guard !isRunning else { return }
        isRunning = true

        let argString = (["sync"] + extraArgs)
            .map { "'\($0.replacingOccurrences(of: "'", with: "'\\''"))'" }
            .joined(separator: " ")
        let command = "\"\(Paths.wrapper.path)\" \(argString)"

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = ["-lc", command]
        proc.currentDirectoryURL = Paths.repoDir

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
                .replacingOccurrences(
                    of: "\u{1B}\\[[0-9;]*m", with: "", options: .regularExpression)
            DispatchQueue.main.async { onLine?(text) }
        }

        proc.terminationHandler = { [weak self] p in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                self?.isRunning = false
                self?.process = nil
                onExit?(p.terminationStatus)
            }
        }

        do {
            try proc.run()
            process = proc
        } catch {
            isRunning = false
            DispatchQueue.main.async {
                onLine?("Failed to launch sync: \(error)\n")
                onExit?(-1)
            }
        }
    }

    /// Blocking variant for the headless `--sync-now` launchd path. Returns the
    /// child's exit code; streams output to stdout/stderr untouched.
    static func runHeadless(extraArgs: [String] = []) -> Int32 {
        let argString = (["sync"] + extraArgs)
            .map { "'\($0.replacingOccurrences(of: "'", with: "'\\''"))'" }
            .joined(separator: " ")
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/bash")
        proc.arguments = ["-lc", "\"\(Paths.wrapper.path)\" \(argString)"]
        proc.currentDirectoryURL = Paths.repoDir
        do {
            try proc.run()
            proc.waitUntilExit()
            return proc.terminationStatus
        } catch {
            FileHandle.standardError.write(Data("mikoshi --sync-now: \(error)\n".utf8))
            return -1
        }
    }
}
