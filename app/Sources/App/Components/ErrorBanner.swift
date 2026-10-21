import SwiftUI

// ErrorBanner.swift — the one error/result presentation surface. Maps every
// BridgeError to a banner with a code chip, message, collapsible details, and
// friendly guidance for the known pain points (local-network permission,
// pairing approval). Forms keep failures inline; global ones go through
// AppState.banner.

struct BannerModel: Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        case error
        case info
        case warning
    }

    var kind: Kind
    var code: String?
    var message: String
    var details: String?
    var guidance: String?

    static func info(_ message: String) -> BannerModel {
        BannerModel(kind: .info, code: nil, message: message, details: nil, guidance: nil)
    }
}

enum ErrorPresentation {
    static let localNetworkGuidance =
        "macOS is blocking local-network access. Open System Settings → "
        + "Privacy & Security → Local Network and enable SteamOS Devkit, then retry."

    static let pairingDeclinedGuidance =
        "Pairing was declined on the device. Start pairing again and approve "
        + "the request on your Steam Deck when it appears."

    static let pairingTimeoutGuidance =
        "The device did not approve pairing in time. Start pairing again and "
        + "approve the request on your Steam Deck within two minutes."

    /// Maps an error to a banner, or nil when the error is mere task
    /// cancellation — `.task` cancels in-flight work whenever its view
    /// disappears (navigation, sheet dismissal), which is control flow, not a
    /// failure. Every call site must skip presentation for nil.
    static func model(for error: Error) -> BannerModel? {
        if error is CancellationError { return nil }
        let nsError = error as NSError
        if nsError.domain == NSCocoaErrorDomain && nsError.code == NSUserCancelledError {
            return nil
        }
        switch error {
        case BridgeError.devkit(let payload):
            return model(for: payload)
        case BridgeError.rpc(let code, let message):
            return BannerModel(
                kind: .error, code: "RPC \(code)", message: message,
                details: nil, guidance: nil)
        case BridgeError.transport(let message):
            return BannerModel(
                kind: .error, code: "TRANSPORT",
                message: "the devkit-mcp server connection failed: \(message)",
                details: nil, guidance: nil)
        case BridgeError.decoding(let message):
            return BannerModel(
                kind: .error, code: "DECODING",
                message: "the server reply was not understood: \(message)",
                details: nil, guidance: nil)
        case BridgeError.protocolViolation(let message):
            return BannerModel(
                kind: .error, code: "PROTOCOL",
                message: "protocol violation: \(message)", details: nil, guidance: nil)
        default:
            return BannerModel(
                kind: .error, code: nil, message: error.localizedDescription,
                details: nil, guidance: nil)
        }
    }

    static func model(for payload: DevkitErrorPayload) -> BannerModel {
        let guidance: String?
        switch payload.code {
        case ErrorCodes.permissionLocalNetwork:
            guidance = localNetworkGuidance
        case ErrorCodes.pairingDeclined:
            guidance = pairingDeclinedGuidance
        case ErrorCodes.pairingTimeout:
            guidance = pairingTimeoutGuidance
        default:
            guidance = nil
        }
        var details = payload.details.map { JSONValue.object($0).prettyPrinted }
        if let exit = payload.remoteExitCode {
            let line = "remote_exit_code: \(exit)"
            details = details.map { $0 + "\n" + line } ?? line
        }
        return BannerModel(
            kind: .error, code: payload.code, message: payload.message,
            details: details, guidance: guidance)
    }
}

extension JSONValue {
    /// Deterministic multi-line rendering for banner details.
    var prettyPrinted: String {
        switch self {
        case .string(let v): return "\"\(v)\""
        case .int(let v): return String(v)
        case .double(let v): return String(v)
        case .bool(let v): return String(v)
        case .null: return "null"
        case .array(let items):
            if items.isEmpty { return "[]" }
            return "[" + items.map(\.prettyPrinted).joined(separator: ", ") + "]"
        case .object(let dict):
            if dict.isEmpty { return "{}" }
            return dict.keys.sorted()
                .map { "\($0): \(dict[$0]?.prettyPrinted ?? "null")" }
                .joined(separator: "\n")
        }
    }
}

struct ErrorBanner: View {
    let model: BannerModel
    var onDismiss: (() -> Void)?
    @State private var showDetails = false

    private var tint: Color {
        switch model.kind {
        case .error: return .red
        case .info: return .green
        case .warning: return .yellow
        }
    }

    private var icon: String {
        switch model.kind {
        case .error: return "exclamationmark.triangle.fill"
        case .info: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.circle.fill"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: icon)
                    .foregroundStyle(tint)
                if let code = model.code {
                    Text(code)
                        .font(.caption.weight(.semibold))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(tint.opacity(0.15), in: Capsule())
                        .foregroundStyle(tint)
                }
                Text(model.message)
                    .font(.callout)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let onDismiss {
                    Button(action: onDismiss) {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .help("Dismiss")
                }
            }
            if let guidance = model.guidance {
                Text(guidance)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            if let details = model.details, !details.isEmpty {
                DisclosureGroup("Details", isExpanded: $showDetails) {
                    Text(details)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(6)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 6))
                }
                .font(.caption)
            }
        }
        .padding(10)
        .background(tint.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8).strokeBorder(tint.opacity(0.3))
        )
    }
}

struct ErrorBanner_Previews: PreviewProvider {
    static var previews: some View {
        VStack {
            ErrorBanner(model: ErrorPresentation.model(for: DevkitErrorPayload(
                code: ErrorCodes.permissionLocalNetwork,
                message: "local network permission denied (errno 65)",
                details: ["errno": .int(65)], remoteExitCode: nil)))
            ErrorBanner(model: .info("title zz_devkit_test configured"))
        }
        .padding()
        .frame(width: 520)
    }
}
