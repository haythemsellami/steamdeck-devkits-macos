import Foundation

// Validation.swift — client-side guards that prevent obvious mistakes only.
// The server remains the authority; VALIDATION errors from it render through
// the same banner path. Mirrors constants.py: GAMEID_REGEX and
// RESERVED_GAMEIDS exactly.

enum Validation {
    /// constants.py RESERVED_GAMEIDS — rejected with an explanation.
    static let reservedGameIDs: Set<String> = ["steam", "steamdeckard", "steamvr", "steamvrdeckard"]

    /// constants.py GAMEID_REGEX.
    static let gameIDPattern = #"^[A-Za-z_][A-Za-z0-9_.]+$"#

    /// Returns nil when valid, else a user-facing reason.
    static func gameIDError(_ raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespaces)
        if value.isEmpty { return "gameid is required" }
        if reservedGameIDs.contains(value) {
            return "“\(value)” is reserved by SteamOS itself — pick another gameid"
        }
        if value.range(of: gameIDPattern, options: .regularExpression) == nil {
            return "use letters, digits, “_” and “.”; the first character must be a letter or “_”"
        }
        return nil
    }

    /// Returns nil when valid IPv4 (dotted quads, each octet 0–255).
    static func ipv4Error(_ raw: String) -> String? {
        let value = raw.trimmingCharacters(in: .whitespaces)
        if value.isEmpty { return "address is required" }
        let octets = value.split(separator: ".", omittingEmptySubsequences: false)
        guard octets.count == 4 else {
            return "enter a dotted IPv4 address, e.g. 192.0.2.10"
        }
        for octet in octets {
            guard let n = Int(octet), (0...255).contains(n), String(n) == String(octet) else {
                return "“\(octet)” is not a valid IPv4 octet (0–255)"
            }
        }
        return nil
    }
}
