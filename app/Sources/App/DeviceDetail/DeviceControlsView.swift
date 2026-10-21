import SwiftUI

// DeviceControlsView.swift — steam mode, session, reload, profiling toggles,
// renderdoc server, proton logging. Each action is a task whose outcome
// (DevkitResult.message or the thrown BridgeError) shows in one banner.

@MainActor
@Observable
final class DeviceControlsViewModel {
    let bridge: any DevkitBridge
    let appState: AppState
    let deviceName: String
    /// Called after actions that change device state (steam mode, session,
    /// reload) so the status card refreshes.
    var onStatusChanged: (() async -> Void)?

    var steamMode: SteamMode = .os
    var steamModeGameID = ""
    var steamModeArgs = ""
    var gdbserver = false
    var session: DeviceSession = .gamescope
    var protonMode: ProtonLogMode = .off
    var winedebug = ""
    var perfOverlayOn = false
    var frametimesOn = false
    var renderdocLayerOn = false
    var renderdocServerStatus: String?

    /// Name of the action currently running, if any.
    private(set) var runningAction: String?
    var outcome: BannerModel?

    init(
        bridge: any DevkitBridge, appState: AppState, deviceName: String,
        onStatusChanged: (() async -> Void)? = nil
    ) {
        self.bridge = bridge
        self.appState = appState
        self.deviceName = deviceName
        self.onStatusChanged = onStatusChanged
    }

    var isRunning: Bool { runningAction != nil }

    /// Runs one control action and renders its result. `refreshesStatus`
    /// triggers a status reload after success.
    private func run(
        _ name: String, refreshesStatus: Bool = false,
        _ operation: () async throws -> DevkitResult
    ) async {
        guard runningAction == nil else { return }
        runningAction = name
        defer { runningAction = nil }
        do {
            let result = try await operation()
            outcome = .info(result.message ?? "done")
            if refreshesStatus { await onStatusChanged?() }
        } catch {
            if let model = ErrorPresentation.model(for: error) {
                outcome = model
            }
        }
    }

    func applySteamMode() async {
        let gameID = steamModeGameID.trimmingCharacters(in: .whitespaces)
        let args = steamModeArgs.trimmingCharacters(in: .whitespaces)
        await run("steam mode", refreshesStatus: true) {
            try await bridge.setSteamMode(
                device: deviceName, mode: steamMode,
                gameid: gameID.isEmpty ? nil : gameID,
                args: args.isEmpty ? nil : args,
                gdbserver: gdbserver)
        }
    }

    func applySession() async {
        await run("session", refreshesStatus: true) {
            try await bridge.setSession(device: deviceName, session: session)
        }
    }

    func reloadSteam() async {
        await run("reload steam", refreshesStatus: true) {
            try await bridge.reloadSteam(device: deviceName)
        }
    }

    func setPerfOverlay(_ enabled: Bool) async {
        await run("perf overlay") {
            try await bridge.perfOverlay(device: deviceName, enabled: enabled)
        }
    }

    func setFrametimes(_ enabled: Bool) async {
        await run("frametimes") {
            try await bridge.frametimes(device: deviceName, enabled: enabled)
        }
    }

    func setRenderdocLayer(_ enabled: Bool) async {
        await run("renderdoc layer", refreshesStatus: true) {
            try await bridge.renderdocLayer(device: deviceName, enabled: enabled)
        }
    }

    func renderdocServer(_ action: RenderdocServerAction) async {
        await run("renderdoc server \(action.rawValue)", refreshesStatus: action != .status) {
            let result = try await bridge.renderdocServer(device: deviceName, action: action)
            renderdocServerStatus = result.message
            return result
        }
    }

    func applyProtonLogging() async {
        let debug = winedebug.trimmingCharacters(in: .whitespaces)
        await run("proton logging") {
            try await bridge.protonLogging(
                device: deviceName, mode: protonMode,
                winedebug: debug.isEmpty ? nil : debug)
        }
    }
}

struct DeviceControlsView: View {
    @Bindable var model: DeviceControlsViewModel

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 16) {
                if let outcome = model.outcome {
                    ErrorBanner(model: outcome) { model.outcome = nil }
                }
                steamModeSection
                sessionSection
                profilingSection
                renderdocSection
                protonSection
            }
            .disabled(model.isRunning)
        } label: {
            Label("Device Controls", systemImage: "slider.horizontal.3")
        }
    }

    private var steamModeSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Steam mode").font(.headline)
            Picker("Mode", selection: $model.steamMode) {
                Text("OS (gaming mode)").tag(SteamMode.os)
                Text("OS_DEV (development)").tag(SteamMode.osDev)
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 380)
            HStack(spacing: 8) {
                TextField("gameid to run (optional)", text: $model.steamModeGameID)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 200)
                TextField("extra steam args (optional)", text: $model.steamModeArgs)
                    .textFieldStyle(.roundedBorder)
                Toggle("gdbserver", isOn: $model.gdbserver)
            }
            Button(action: { Task { await model.applySteamMode() } }) {
                if model.runningAction == "steam mode" {
                    ProgressView().controlSize(.small)
                } else {
                    Text("Apply steam mode")
                }
            }
        }
    }

    private var sessionSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Session").font(.headline)
            HStack(spacing: 8) {
                Picker("Session", selection: $model.session) {
                    ForEach(DeviceSession.allCases, id: \.self) { session in
                        Text(session.rawValue).tag(session)
                    }
                }
                .frame(maxWidth: 220)
                Button("Apply session") { Task { await model.applySession() } }
                Button("Reload Steam") { Task { await model.reloadSteam() } }
            }
            Text("Session changes take effect after a steam reload.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var profilingSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Profiling").font(.headline)
            HStack(spacing: 16) {
                Toggle("Performance overlay", isOn: $model.perfOverlayOn)
                    .onChange(of: model.perfOverlayOn) { _, newValue in
                        Task { await model.setPerfOverlay(newValue) }
                    }
                Toggle("Frametimes capture", isOn: $model.frametimesOn)
                    .onChange(of: model.frametimesOn) { _, newValue in
                        Task { await model.setFrametimes(newValue) }
                    }
                Toggle("RenderDoc layer", isOn: $model.renderdocLayerOn)
                    .onChange(of: model.renderdocLayerOn) { _, newValue in
                        Task { await model.setRenderdocLayer(newValue) }
                    }
            }
        }
    }

    private var renderdocSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("RenderDoc replay server").font(.headline)
            HStack(spacing: 8) {
                Button("Start") { Task { await model.renderdocServer(.start) } }
                Button("Stop") { Task { await model.renderdocServer(.stop) } }
                Button("Status") { Task { await model.renderdocServer(.status) } }
                if let status = model.renderdocServerStatus {
                    Text(status)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var protonSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Proton logging").font(.headline)
            HStack(spacing: 8) {
                Picker("Proton logging", selection: $model.protonMode) {
                    Text("off").tag(ProtonLogMode.off)
                    Text("on").tag(ProtonLogMode.on)
                }
                .pickerStyle(.segmented)
                .frame(width: 140)
                TextField("winedebug channels (optional)", text: $model.winedebug)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 260)
                Button("Apply") { Task { await model.applyProtonLogging() } }
            }
        }
    }
}

struct DeviceControlsView_Previews: PreviewProvider {
    static var previews: some View {
        let appState = AppState.preview()
        return DeviceControlsView(model: DeviceControlsViewModel(
            bridge: appState.bridge, appState: appState, deviceName: "steamdeck"))
        .padding()
        .frame(width: 640)
    }
}
