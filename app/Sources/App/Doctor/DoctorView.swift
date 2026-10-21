import SwiftUI

// DoctorView.swift — host readiness: doctor() report plus the local-network
// inference from capabilities(). When Local Network is blocked (or a doctor
// check says so) a prominent banner carries the remediation path.

@MainActor
@Observable
final class DoctorViewModel {
    let bridge: any DevkitBridge
    let appState: AppState

    var isLoading = false
    var error: BannerModel?

    init(bridge: any DevkitBridge, appState: AppState) {
        self.bridge = bridge
        self.appState = appState
    }

    var report: DoctorReport? {
        appState.doctorReport
    }

    var localNetwork: LocalNetworkInference? {
        appState.hostLocalNetwork
    }

    /// Blocked when the host inference says so, or any doctor check about the
    /// local network reports an error.
    var localNetworkBlocked: Bool {
        if localNetwork == .blocked { return true }
        return report?.checks.contains {
            $0.name.localizedCaseInsensitiveContains("network")
                && $0.status == .error
        } ?? false
    }

    var failingChecks: [DoctorCheck] {
        report?.checks.filter { $0.status == .error || $0.status == .warning } ?? []
    }

    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        error = nil
        await appState.refreshDoctor()
        if appState.doctorReport == nil, let banner = appState.banner {
            error = banner
            appState.dismissBanner()
        }
    }
}

struct DoctorView: View {
    @Environment(AppState.self) private var appState
    @State private var model: DoctorViewModel?

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Running doctor…")
            }
        }
        .navigationTitle("Doctor")
        .task {
            if model == nil {
                let vm = DoctorViewModel(bridge: appState.bridge, appState: appState)
                model = vm
                await vm.refresh()
            }
        }
    }

    @ViewBuilder
    private func content(_ model: DoctorViewModel) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if model.localNetworkBlocked {
                    ErrorBanner(model: BannerModel(
                        kind: .error,
                        code: ErrorCodes.permissionLocalNetwork,
                        message: "Local Network access is blocked for SteamOS Devkit.",
                        details: nil,
                        guidance: ErrorPresentation.localNetworkGuidance))
                }
                if let error = model.error {
                    ErrorBanner(model: error) { model.error = nil }
                }
                if let report = model.report {
                    statusBanner(report)
                    contextCard(report)
                    toolsCard(report)
                    checksCard(report)
                } else if model.isLoading {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Checking host…").foregroundStyle(.secondary)
                    }
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button(action: { Task { await model.refresh() } }) {
                    Label("Run again", systemImage: "arrow.clockwise")
                }
                .disabled(model.isLoading)
            }
        }
    }

    @ViewBuilder
    private func statusBanner(_ report: DoctorReport) -> some View {
        HStack(spacing: 10) {
            Image(systemName: report.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                .font(.title2)
                .foregroundStyle(report.ok ? .green : .orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(report.ok ? "This Mac is ready for devkit work" : "Some checks need attention")
                    .font(.headline)
                Text("\(report.platform) — \(report.arch) — context \(report.context.rawValue)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            (report.ok ? Color.green : Color.orange).opacity(0.08),
            in: RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder
    private func contextCard(_ report: DoctorReport) -> some View {
        GroupBox("Host") {
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                GridRow {
                    Text("platform").foregroundStyle(.secondary)
                    Text("\(report.platform) (\(report.arch))")
                }
                GridRow {
                    Text("python").foregroundStyle(.secondary)
                    Text(report.pythonVersion)
                }
                GridRow {
                    Text("state dir").foregroundStyle(.secondary)
                    Text(report.stateDir)
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                }
            }
            .font(.callout)
        }
    }

    @ViewBuilder
    private func toolsCard(_ report: DoctorReport) -> some View {
        GroupBox("Tools") {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    Image(systemName: report.tools.rsync.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .foregroundStyle(report.tools.rsync.ok ? .green : .red)
                    Text("rsync")
                        .frame(width: 70, alignment: .leading)
                    Text(
                        [
                            report.tools.rsync.path,
                            report.tools.rsync.version,
                            report.tools.rsync.flavor?.rawValue,
                            "(\(report.tools.rsync.source.rawValue))",
                        ]
                        .compactMap { $0 }
                        .joined(separator: " ")
                    )
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    if let message = report.tools.rsync.message, !report.tools.rsync.ok {
                        Text("— \(message)")
                            .foregroundStyle(.secondary)
                    }
                }
                HStack(spacing: 8) {
                    Image(systemName: report.tools.sshPath != nil ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .foregroundStyle(report.tools.sshPath != nil ? .green : .red)
                    Text("ssh")
                        .frame(width: 70, alignment: .leading)
                    Text([report.tools.sshPath, report.tools.sshVersion].compactMap { $0 }.joined(separator: " "))
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                }
                HStack(spacing: 8) {
                    Image(systemName: report.tools.terminal != nil ? "checkmark.circle.fill" : "minus.circle")
                        .foregroundStyle(report.tools.terminal != nil ? .green : .secondary)
                    Text("terminal")
                        .frame(width: 70, alignment: .leading)
                    Text(report.tools.terminal ?? "none found")
                        .font(.system(.callout, design: .monospaced))
                }
            }
            .font(.callout)
        }
    }

    @ViewBuilder
    private func checksCard(_ report: DoctorReport) -> some View {
        GroupBox("Checks") {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(report.checks) { check in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 8) {
                            Image(systemName: icon(for: check.status))
                                .foregroundStyle(color(for: check.status))
                                .frame(width: 16)
                            Text(check.name)
                                .font(.system(.callout, design: .monospaced))
                                .frame(width: 160, alignment: .leading)
                            Text(check.summary)
                                .foregroundStyle(.secondary)
                        }
                        if !check.details.isEmpty {
                            DisclosureGroup("Details") {
                                VStack(alignment: .leading, spacing: 2) {
                                    ForEach(check.details.keys.sorted(), id: \.self) { key in
                                        Text("\(key): \(check.details[key] ?? "")")
                                            .font(.system(.caption, design: .monospaced))
                                            .textSelection(.enabled)
                                    }
                                }
                                .padding(.top, 2)
                            }
                            .font(.caption)
                            .padding(.leading, 24)
                        }
                    }
                }
            }
            .font(.callout)
        }
    }

    private func icon(for status: DoctorCheckStatus) -> String {
        switch status {
        case .ok: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        case .unknown: return "questionmark.circle"
        }
    }

    private func color(for status: DoctorCheckStatus) -> Color {
        switch status {
        case .ok: return .green
        case .warning: return .orange
        case .error: return .red
        case .unknown: return .secondary
        }
    }
}

struct DoctorView_Previews: PreviewProvider {
    static var previews: some View {
        DoctorView()
            .environment(AppState.preview())
    }
}
