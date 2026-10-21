import SwiftUI

// DeviceDetailView.swift — one device: status card, capabilities (honest
// per-operation labels), controls, and the titles tab. Owns status/
// capabilities loading and feeds AppState's reachability cache.

@MainActor
@Observable
final class DeviceDetailViewModel {
    enum Tab: String, CaseIterable, Identifiable {
        case overview = "Overview"
        case titles = "Titles"
        var id: String { rawValue }
    }

    let bridge: any DevkitBridge
    let appState: AppState
    let deviceName: String

    var status: DeviceStatus?
    var capabilities: Capabilities?
    var isLoading = false
    var loadError: BannerModel?

    init(bridge: any DevkitBridge, appState: AppState, deviceName: String) {
        self.bridge = bridge
        self.appState = appState
        self.deviceName = deviceName
    }

    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            status = try await bridge.status(device: deviceName)
            appState.markReachable(deviceName, true)
            loadError = nil
        } catch {
            appState.markReachable(deviceName, false)
            loadError = ErrorPresentation.model(for: error)
            return
        }
        capabilities = try? await bridge.capabilities(device: deviceName)
    }
}

struct DeviceDetailView: View {
    @Environment(AppState.self) private var appState
    @State private var model: DeviceDetailViewModel?
    @State private var tab: DeviceDetailViewModel.Tab

    let deviceName: String

    init(deviceName: String, initialTab: DeviceDetailViewModel.Tab = .overview) {
        self.deviceName = deviceName
        _tab = State(initialValue: initialTab)
    }

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Connecting to \(deviceName)…")
            }
        }
        .task {
            if model == nil {
                let vm = DeviceDetailViewModel(
                    bridge: appState.bridge, appState: appState, deviceName: deviceName)
                model = vm
                await vm.refresh()
            }
        }
    }

    @ViewBuilder
    private func content(_ model: DeviceDetailViewModel) -> some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            Picker("Section", selection: $tab) {
                ForEach(DeviceDetailViewModel.Tab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .frame(width: 220)
            .padding(.vertical, 8)

            switch tab {
            case .overview:
                overview(model)
            case .titles:
                TitlesView(deviceName: deviceName)
            }
        }
        .navigationTitle(deviceName)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: { appState.showsDeploySheet = true }) {
                    Label("Deploy build…", systemImage: "arrow.up.doc")
                }
            }
            ToolbarItem(placement: .automatic) {
                Button(action: { Task { await model.refresh() } }) {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(model.isLoading)
            }
        }
    }

    @ViewBuilder
    private func overview(_ model: DeviceDetailViewModel) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let error = model.loadError {
                    ErrorBanner(model: error) {
                        Task { await model.refresh() }
                    }
                }
                if let status = model.status {
                    statusCard(status)
                } else if model.isLoading {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Reading device status…").foregroundStyle(.secondary)
                    }
                }
                if let capabilities = model.capabilities {
                    capabilitiesCard(capabilities)
                }
                DeviceControlsView(model: DeviceControlsViewModel(
                    bridge: model.bridge, appState: model.appState, deviceName: model.deviceName,
                    onStatusChanged: { [weak model] in await model?.refresh() }))
            }
            .padding()
        }
    }

    @ViewBuilder
    private func statusCard(_ status: DeviceStatus) -> some View {
        GroupBox {
            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 6) {
                GridRow {
                    Text("hostname").foregroundStyle(.secondary)
                    Text(status.hostname).textSelection(.enabled)
                }
                GridRow {
                    Text("system").foregroundStyle(.secondary)
                    Text("\(status.osName) \(status.osVersion) — \(status.isDeckard ? "Steam Frame" : "Steam Deck")")
                }
                GridRow {
                    Text("session").foregroundStyle(.secondary)
                    Text(status.sessionStatus)
                    Text("steam").foregroundStyle(.secondary)
                    Text(status.steamStatus.rawValue.replacingOccurrences(of: "_", with: " "))
                }
                GridRow {
                    Text("steam branch").foregroundStyle(.secondary)
                    Text(status.steamOsclientBranch ?? "?")
                    Text("client").foregroundStyle(.secondary)
                    Text(status.steamOsclientVersion?.description ?? "?")
                }
                GridRow {
                    Text("cef debugging").foregroundStyle(.secondary)
                    Text(status.cefDebuggingEnabled ? "on" : "off")
                    Text("user password").foregroundStyle(.secondary)
                    Text(status.userPasswordIsSet ? "set" : "not set")
                }
                GridRow {
                    Text("renderdoc layer").foregroundStyle(.secondary)
                    Text(status.renderdocLayerEnabled ? "on" : "off")
                    Text("renderdoc server").foregroundStyle(.secondary)
                    Text(status.renderdocReplayServerRunning ? "running" : "stopped")
                }
                GridRow {
                    Text("steam args").foregroundStyle(.secondary)
                    Text(argsSummary(current: status.steamCurrentArgs, default: status.steamDefaultArgs))
                        .font(.system(.callout, design: .monospaced))
                        .textSelection(.enabled)
                }
            }
            .font(.callout)
        } label: {
            Label("Status", systemImage: "info.circle")
        }
    }

    private func argsSummary(current: [String], default defaultArgs: [String]) -> String {
        let currentText = current.isEmpty ? "(none)" : current.joined(separator: " ")
        if current == defaultArgs { return "\(currentText) — same as default" }
        let defaultText = defaultArgs.isEmpty ? "(none)" : defaultArgs.joined(separator: " ")
        return "\(currentText) — default: \(defaultText)"
    }

    @ViewBuilder
    private func capabilitiesCard(_ capabilities: Capabilities) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 10) {
                if let device = capabilities.device {
                    HStack(spacing: 16) {
                        Label(
                            "gpu-trace \(triLabel(device.hasGpuTrace))",
                            systemImage: "waveform.path.ecg")
                        Label(
                            "mangohudctl \(triLabel(device.hasMangohudctl))",
                            systemImage: "speedometer")
                        Label(
                            "session-select: \(device.sessionSelect.rawValue)",
                            systemImage: "rectangle.2.swap")
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                DisclosureGroup("Per-operation capability") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(capabilities.operations.keys.sorted(), id: \.self) { name in
                            HStack {
                                Text(name)
                                    .font(.system(.callout, design: .monospaced))
                                    .frame(width: 200, alignment: .leading)
                                OperationStatusLabel(operation: capabilities.operations[name]!)
                            }
                        }
                    }
                    .padding(.top, 6)
                }
            }
        } label: {
            Label("Capabilities", systemImage: "checklist")
        }
    }

    private func triLabel(_ value: Bool?) -> String {
        switch value {
        case .some(true): return "yes"
        case .some(false): return "no"
        case .none: return "?"
        }
    }
}

struct DeviceDetailView_Previews: PreviewProvider {
    static var previews: some View {
        DeviceDetailView(deviceName: "steamdeck")
            .environment(AppState.preview())
    }
}
