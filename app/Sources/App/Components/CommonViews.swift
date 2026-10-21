import SwiftUI

// CommonViews.swift — badges, honest capability labels, formatters, and the
// key/value row editor shared by the title-config and deploy forms.

enum Format {
    static func bytes(_ value: Int?) -> String {
        guard let value else { return "—" }
        return ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .file)
    }

    static func rate(_ bytesPerSecond: Double?) -> String {
        guard let bytesPerSecond else { return "" }
        return ByteCountFormatter.string(fromByteCount: Int64(bytesPerSecond), countStyle: .file) + "/s"
    }

    static func date(_ value: Date?) -> String {
        guard let value else { return "—" }
        return value.formatted(date: .abbreviated, time: .shortened)
    }

    static func lastSeen(_ value: Date?) -> String {
        guard let value else { return "never" }
        return value.formatted(.relative(presentation: .named))
    }

    /// Job duration: started→finished (or →now while running).
    static func duration(of job: JobInfo, now: Date = Date()) -> String {
        guard let start = job.startedAt else { return "—" }
        let end = job.finishedAt ?? now
        let seconds = max(0, Int(end.timeIntervalSince(start)))
        if seconds < 60 { return "\(seconds)s" }
        return "\(seconds / 60)m \(seconds % 60)s"
    }
}

struct StateBadge: View {
    let state: JobState

    private var color: Color {
        switch state {
        case .queued: return .secondary
        case .running: return .blue
        case .succeeded: return .green
        case .failed, .timeout: return .red
        case .cancelled, .interrupted: return .orange
        }
    }

    var body: some View {
        Text(state.rawValue.replacingOccurrences(of: "_", with: " "))
            .font(.caption.weight(.medium))
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }
}

struct FamilyBadge: View {
    let family: DeviceFamily

    var body: some View {
        Text(label)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(Color.secondary.opacity(0.15), in: Capsule())
            .foregroundStyle(.secondary)
    }

    private var label: String {
        switch family {
        case .deck: return "Deck"
        case .frame: return "Frame"
        case .unknown: return "device"
        }
    }
}

struct ViaBadge: View {
    let via: DiscoveryVia

    var body: some View {
        Label(via == .mdns ? "mDNS" : "manual", systemImage: via == .mdns ? "bonjour" : "network")
            .font(.caption2)
            .foregroundStyle(.secondary)
    }
}

/// Reachability dot: green reachable, red failed, gray unknown.
struct ReachabilityDot: View {
    /// nil = unknown/never probed.
    let reachable: Bool?

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .help(help)
    }

    private var color: Color {
        switch reachable {
        case .some(true): return .green
        case .some(false): return .red
        case .none: return .gray.opacity(0.5)
        }
    }

    private var help: String {
        switch reachable {
        case .some(true): return "reachable"
        case .some(false): return "unreachable"
        case .none: return "not probed yet"
        }
    }
}

/// Honest capability wording — never stronger than the server's verdict.
struct OperationStatusLabel: View {
    let operation: OperationCapability

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .frame(width: 14)
            Text(Self.text(for: operation.status))
                .foregroundStyle(.primary)
            if let reason = operation.reason {
                Text("— \(reason)")
                    .foregroundStyle(.secondary)
            }
            if let dependency = operation.dependency {
                Text("— needs \(dependency)")
                    .foregroundStyle(.secondary)
            }
        }
        .font(.callout)
    }

    static func text(for status: OperationStatus) -> String {
        switch status {
        case .supportedTested: return "supported — tested on hardware"
        case .supportedUntested: return "supported — untested on hardware"
        case .dependencyRequired: return "dependency required"
        case .captureExportOnly: return "capture/export only"
        case .unavailableHost: return "unavailable on this host"
        case .unavailableDevice: return "unavailable on this device"
        case .unsupportedUpstream: return "unsupported by upstream"
        }
    }

    private var icon: String {
        switch operation.status {
        case .supportedTested: return "checkmark.circle.fill"
        case .supportedUntested: return "checkmark.circle"
        case .dependencyRequired: return "shippingbox"
        case .captureExportOnly: return "square.and.arrow.up"
        case .unavailableHost, .unavailableDevice: return "minus.circle"
        case .unsupportedUpstream: return "xmark.circle"
        }
    }

    private var color: Color {
        switch operation.status {
        case .supportedTested: return .green
        case .supportedUntested: return .blue
        case .dependencyRequired, .captureExportOnly: return .orange
        case .unavailableHost, .unavailableDevice, .unsupportedUpstream: return .secondary
        }
    }
}

struct JobKindIcon: View {
    let kind: JobKind

    var body: some View {
        Image(systemName: symbol)
            .foregroundStyle(.secondary)
            .help(label)
    }

    private var symbol: String {
        switch kind {
        case .deploy: return "arrow.up.doc"
        case .fetchLogs: return "doc.text"
        case .screenshot: return "camera"
        case .controllerConfig: return "gamecontroller"
        case .gpuTrace: return "waveform.path.ecg"
        case .rgp: return "record.circle"
        }
    }

    private var label: String {
        switch kind {
        case .deploy: return "deploy"
        case .fetchLogs: return "fetch logs"
        case .screenshot: return "screenshot"
        case .controllerConfig: return "controller config"
        case .gpuTrace: return "GPU trace"
        case .rgp: return "RGP capture"
        }
    }
}

struct ArtifactKindIcon: View {
    let kind: ArtifactKind

    var body: some View {
        Image(systemName: symbol)
            .foregroundStyle(.secondary)
    }

    private var symbol: String {
        switch kind {
        case .logs, .protonLog: return "doc.text"
        case .screenshot: return "camera"
        case .controllerConfig: return "gamecontroller"
        case .gpuTrace: return "waveform.path.ecg"
        case .rgp: return "record.circle"
        case .frametimes: return "chart.xyaxis.line"
        case .dumps: return "externaldrive"
        }
    }
}

/// Editable key/value rows used by the title-config and deploy forms.
/// Empty rows are dropped by the caller when building the DTO.
struct KeyValueEditor: View {
    @Binding var rows: [KeyValueRow]
    var keyPlaceholder: String = "KEY"
    var valuePlaceholder: String = "value"

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach($rows) { $row in
                HStack(spacing: 6) {
                    TextField(keyPlaceholder, text: $row.key)
                        .font(.system(.callout, design: .monospaced))
                        .frame(minWidth: 140)
                    Text("=")
                        .foregroundStyle(.secondary)
                    TextField(valuePlaceholder, text: $row.value)
                        .font(.system(.callout, design: .monospaced))
                    Button(action: { rows.removeAll { $0.id == row.id } }) {
                        Image(systemName: "minus.circle")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .help("Remove row")
                }
            }
            Button(action: { rows.append(KeyValueRow()) }) {
                Label("Add", systemImage: "plus.circle")
                    .font(.callout)
            }
            .buttonStyle(.plain)
            .foregroundStyle(.tint)
        }
    }
}

struct KeyValueRow: Identifiable, Equatable, Sendable {
    var id = UUID()
    var key: String = ""
    var value: String = ""

    static func rows(from dict: [String: String]) -> [KeyValueRow] {
        dict.keys.sorted().map { KeyValueRow(key: $0, value: dict[$0] ?? "") }
    }

    /// Drops rows with an empty key (whitespace-trimmed).
    static func dict(from rows: [KeyValueRow]) -> [String: String] {
        var result: [String: String] = [:]
        for row in rows {
            let key = row.key.trimmingCharacters(in: .whitespaces)
            if !key.isEmpty { result[key] = row.value }
        }
        return result
    }
}
