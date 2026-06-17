import Foundation

/// Minimal blocking subprocess helper for short commands (security, launchctl).
/// For the long-running sync we use `SyncRunner` instead.
enum Shell {
    struct Result {
        let status: Int32
        let stdout: String
        let stderr: String
    }

    @discardableResult
    static func run(_ launchPath: String, _ args: [String],
                    cwd: URL? = nil, env: [String: String]? = nil) -> Result {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: launchPath)
        proc.arguments = args
        if let cwd { proc.currentDirectoryURL = cwd }
        if let env { proc.environment = env }
        let out = Pipe(), err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        do {
            try proc.run()
        } catch {
            return Result(status: -1, stdout: "", stderr: "\(error)")
        }
        let outData = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return Result(
            status: proc.terminationStatus,
            stdout: String(decoding: outData, as: UTF8.self),
            stderr: String(decoding: errData, as: UTF8.self))
    }
}
