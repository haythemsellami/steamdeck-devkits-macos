import Foundation

// ErrorPresentationTests.swift — error → banner mapping: code chip, message,
// details rendering, the special-cased guidance for local-network permission
// and pairing approval, and the cancellation filter (task cancellation from
// `.task` view lifecycle is control flow, never a banner).

enum ErrorPresentationTests {
    static func devkitPayloadMapsCodeAndDetails() throws {
        let payload = DevkitErrorPayload(
            code: ErrorCodes.deviceUnreachable,
            message: "host did not answer ssh",
            details: ["host": .string("192.0.2.10"), "errno": .int(65)],
            remoteExitCode: 255)
        let banner = try requireBanner(ErrorPresentation.model(for: BridgeError.devkit(payload)))
        try expectEqual(banner.kind, .error, "devkit errors are error banners")
        try expectEqual(banner.code, ErrorCodes.deviceUnreachable, "code chip")
        try expectEqual(banner.message, "host did not answer ssh", "message")
        guard let details = banner.details else {
            throw VMTestFailure(message: "details rendered")
        }
        try expect(details.contains("host: \"192.0.2.10\""), "details render JSONValue entries")
        try expect(details.contains("remote_exit_code: 255"), "remote exit code appended")
    }

    static func localNetworkPermissionHasRemediation() throws {
        let payload = DevkitErrorPayload(
            code: ErrorCodes.permissionLocalNetwork,
            message: "local network permission denied",
            details: nil, remoteExitCode: nil)
        let banner = try requireBanner(ErrorPresentation.model(for: BridgeError.devkit(payload)))
        guard let guidance = banner.guidance else {
            throw VMTestFailure(message: "guidance present")
        }
        try expect(
            guidance.contains("System Settings") && guidance.contains("Local Network"),
            "guidance points at System Settings → Privacy & Security → Local Network")
        try expect(guidance.contains("SteamOS Devkit"), "guidance names the app")
    }

    static func pairingErrorsHaveFriendlyWording() throws {
        for code in [ErrorCodes.pairingDeclined, ErrorCodes.pairingTimeout] {
            let banner = try requireBanner(ErrorPresentation.model(for: BridgeError.devkit(
                DevkitErrorPayload(
                    code: code, message: "pairing did not complete",
                    details: nil, remoteExitCode: nil))))
            guard let guidance = banner.guidance else {
                throw VMTestFailure(message: "guidance present for \(code)")
            }
            try expect(
                guidance.contains("approve") && guidance.contains("Steam Deck"),
                "\(code) guidance tells the user to approve on the device")
        }
        // An ordinary error has no guidance.
        let plain = try requireBanner(ErrorPresentation.model(for: BridgeError.devkit(
            DevkitErrorPayload(
                code: ErrorCodes.timeout, message: "timed out", details: nil, remoteExitCode: nil))))
        try expectNil(plain.guidance, "generic errors carry no guidance")
    }

    static func transportAndRpcErrorsMap() throws {
        let transport = try requireBanner(
            ErrorPresentation.model(for: BridgeError.transport("broken pipe")))
        try expectEqual(transport.code, "TRANSPORT", "transport code")
        try expect(transport.message.contains("broken pipe"), "transport message carries the reason")
        let rpc = try requireBanner(
            ErrorPresentation.model(for: BridgeError.rpc(code: -32601, message: "method not found")))
        try expectEqual(rpc.code, "RPC -32601", "rpc code chip")
        try expectEqual(rpc.message, "method not found", "rpc message")
    }

    /// The 2026-10-22 user-reported bug: `.task` view-lifecycle cancellation
    /// surfaced as "The operation couldn't be completed. (Swift.CancellationError
    /// error 1.)". Cancellation is control flow — model(for:) returns nil.
    static func cancellationIsNeverPresented() throws {
        try expectNil(
            ErrorPresentation.model(for: CancellationError()),
            "Swift.CancellationError maps to no banner")
        let nsUserCancelled = NSError(
            domain: NSCocoaErrorDomain, code: NSUserCancelledError)
        try expectNil(
            ErrorPresentation.model(for: nsUserCancelled),
            "NSUserCancelledError maps to no banner")
        // A real error still presents (guard against an over-eager filter).
        try expectNotNil(
            ErrorPresentation.model(for: BridgeError.devkit(DevkitErrorPayload(
                code: ErrorCodes.timeout, message: "timed out",
                details: nil, remoteExitCode: nil))),
            "real errors still present")
    }

    private static func requireBanner(_ banner: BannerModel?) throws -> BannerModel {
        guard let banner else { throw VMTestFailure(message: "expected a banner, got nil") }
        return banner
    }
}
