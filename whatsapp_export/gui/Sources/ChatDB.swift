import Foundation
import SQLite3

/// Read-only reader over the live Mac WhatsApp ChatStorage.sqlite, used to
/// populate the favorites chat browser. Mirrors the schema the extractor uses
/// (extract_messages.py: ZWACHATSESSION / ZWAMESSAGE).
struct Chat {
    let jid: String
    let name: String
    let msgCount: Int
    var isGroup: Bool { jid.hasSuffix("@g.us") }
}

enum ChatDB {
    enum LoadError: Error, CustomStringConvertible {
        case notReadable        // FDA almost certainly missing
        case openFailed(String)
        case queryFailed(String)

        var description: String {
            switch self {
            case .notReadable:
                return "ChatStorage.sqlite is not readable — grant Full Disk Access."
            case .openFailed(let m): return "open failed: \(m)"
            case .queryFailed(let m): return "query failed: \(m)"
            }
        }
    }

    /// List chats ordered by message count (desc). Opens the DB read-only and
    /// in immutable mode so we never touch WhatsApp's live file.
    static func listChats() throws -> [Chat] {
        let path = Paths.macChatStorage.path
        guard FileManager.default.isReadableFile(atPath: path) else {
            throw LoadError.notReadable
        }
        var db: OpaquePointer?
        // immutable=1: promise we won't write and the file won't change under
        // us, so SQLite skips locking the WAL — safe for a read-only browse.
        let uri = "file:\(path)?immutable=1"
        let rc = sqlite3_open_v2(
            uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil)
        guard rc == SQLITE_OK, let db else {
            let msg = db.map { String(cString: sqlite3_errmsg($0)) } ?? "rc=\(rc)"
            sqlite3_close(db)
            throw LoadError.openFailed(msg)
        }
        defer { sqlite3_close(db) }

        let sql = """
            SELECT s.ZCONTACTJID AS jid,
                   COALESCE(s.ZPARTNERNAME, s.ZCONTACTJID) AS name,
                   COUNT(m.Z_PK) AS cnt
            FROM ZWACHATSESSION s
            LEFT JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
            WHERE s.ZCONTACTJID IS NOT NULL
            GROUP BY s.Z_PK
            ORDER BY cnt DESC
        """
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
            throw LoadError.queryFailed(String(cString: sqlite3_errmsg(db)))
        }
        defer { sqlite3_finalize(stmt) }

        var chats: [Chat] = []
        while sqlite3_step(stmt) == SQLITE_ROW {
            guard let jidC = sqlite3_column_text(stmt, 0) else { continue }
            let jid = String(cString: jidC)
            let name = sqlite3_column_text(stmt, 1).map { String(cString: $0) } ?? jid
            let cnt = Int(sqlite3_column_int64(stmt, 2))
            chats.append(Chat(jid: jid, name: name, msgCount: cnt))
        }
        return chats
    }
}
