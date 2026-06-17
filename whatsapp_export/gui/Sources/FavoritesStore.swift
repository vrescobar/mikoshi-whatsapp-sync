import Foundation

/// Read/write ~/.mikoshi-favorites.json, faithful to favorites.py's v1 format
/// and its group-never-prune threshold semantics.
///
/// File shape:
/// {
///   "version": 1,
///   "updated_at": "<iso>",
///   "dm_min_messages": <int|null>,
///   "favorites": [ {"jid": ..., "name": ..., "added_at": "<iso>"} ]
/// }
enum FavoritesStore {
    struct Favorite { let jid: String; let name: String; let addedAt: String }

    struct State {
        var dmMinMessages: Int?
        var favorites: [Favorite]
        var jids: Set<String> { Set(favorites.map(\.jid)) }
    }

    static func load() -> State {
        guard let data = try? Data(contentsOf: Paths.favoritesFile),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else {
            return State(dmMinMessages: nil, favorites: [])
        }
        let threshold = obj["dm_min_messages"] as? Int
        var favs: [Favorite] = []
        if let arr = obj["favorites"] as? [[String: Any]] {
            for f in arr {
                guard let jid = f["jid"] as? String else { continue }
                favs.append(Favorite(
                    jid: jid,
                    name: (f["name"] as? String) ?? jid,
                    addedAt: (f["added_at"] as? String) ?? isoNow()))
            }
        }
        return State(dmMinMessages: threshold, favorites: favs)
    }

    /// Persist a new selection.
    ///
    /// `selectedJIDs` is the explicit set the user ticked. `chats` carries the
    /// message counts so we can apply favorites.py's pruning rule: when a DM
    /// threshold is set, DMs already at/above it are redundant (the rule
    /// re-includes them at sync time) and are dropped from the explicit list.
    /// Groups (@g.us) are NEVER pruned.
    static func save(selectedJIDs: Set<String>, dmMinMessages: Int?,
                     chats: [Chat]) {
        let countByJID = Dictionary(uniqueKeysWithValues: chats.map { ($0.jid, $0.msgCount) })
        let nameByJID = Dictionary(uniqueKeysWithValues: chats.map { ($0.jid, $0.name) })
        let prior = load()
        let priorByJID = Dictionary(uniqueKeysWithValues: prior.favorites.map { ($0.jid, $0) })

        var kept: [[String: Any]] = []
        for jid in selectedJIDs {
            let isGroup = jid.hasSuffix("@g.us")
            if let t = dmMinMessages, !isGroup, (countByJID[jid] ?? 0) >= t {
                continue   // redundant DM — the threshold rule covers it
            }
            let addedAt = priorByJID[jid]?.addedAt ?? isoNow()
            let name = nameByJID[jid] ?? priorByJID[jid]?.name ?? jid
            kept.append(["jid": jid, "name": name, "added_at": addedAt])
        }

        var obj: [String: Any] = [
            "version": 1,
            "updated_at": isoNow(),
            "favorites": kept,
        ]
        obj["dm_min_messages"] = dmMinMessages as Any? ?? NSNull()

        guard let data = try? JSONSerialization.data(
            withJSONObject: obj, options: [.prettyPrinted, .withoutEscapingSlashes])
        else { return }
        let url = Paths.favoritesFile
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: .atomic)
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    private static func isoNow() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}
