import SwiftUI

// DiscoverSheet.swift — discovery + pairing. mDNS discovery list with
// per-row Pair, explicit-IP add with live IPv4 validation, and the pairing
// progress view (approval happens on the device; the server waits up to
// 120 s; cancel only abandons the local wait).

@MainActor
@Observable
final class DiscoverViewModel {
    enum PairPhase: Equatable {
        case idle
        case inProgress
        case succeeded(String)
        case failed(BannerModel)
    }

    let bridge: any DevkitBridge
    let appState: AppState

    var discovered: [DiscoveredDevice] = []
    var isDiscovering = false
    var didDiscover = false
    var discoverError: BannerModel?
    var ipAddress = ""
    var pairPhase: PairPhase = .idle
    var pairingTarget = ""

    private var pairTask: Task<Void, Never>?

    init(bridge: any DevkitBridge, appState: AppState) {
        self.bridge = bridge
        self.appState = appState
    }

    /// nil when the field is empty (nothing to complain about yet).
    var ipValidationError: String? {
        guard !ipAddress.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return Validation.ipv4Error(ipAddress)
    }

    var ipIsValid: Bool {
        Validation.ipv4Error(ipAddress) == nil
    }

    func discover() async {
        guard !isDiscovering else { return }
        isDiscovering = true
        discoverError = nil
        defer { isDiscovering = false; didDiscover = true }
        do {
            discovered = try await bridge.discover(timeoutSeconds: 5)
        } catch {
            discovered = []
            if let model = ErrorPresentation.model(for: error) {
                discoverError = model
            }
        }
    }

    func pair(target: String) {
        guard pairTask == nil else { return }
        pairingTarget = target
        pairPhase = .inProgress
        pairTask = Task { [weak self] in
            guard let self else { return }
            defer { pairTask = nil }
            do {
                let result = try await bridge.pair(device: target, timeoutSeconds: 120)
                if Task.isCancelled { pairPhase = .idle; return }
                if result.ok {
                    await appState.refreshDevices()
                    pairPhase = .succeeded(result.message ?? "paired")
                } else {
                    pairPhase = .failed(BannerModel(
                        kind: .error, code: result.code,
                        message: result.message ?? "pairing failed",
                        details: nil, guidance: nil))
                }
            } catch {
                if Task.isCancelled { pairPhase = .idle; return }
                if let model = ErrorPresentation.model(for: error) {
                    pairPhase = .failed(model)
                } else {
                    pairPhase = .idle
                }
            }
        }
    }

    /// Cancels the local wait; the on-device prompt times out on its own.
    func cancelPair() {
        pairTask?.cancel()
        pairTask = nil
        pairPhase = .idle
    }

    func resetPair() {
        pairPhase = .idle
    }
}

struct DiscoverSheet: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var model: DiscoverViewModel?

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Preparing…")
            }
        }
        .frame(minWidth: 560, minHeight: 420)
        .task {
            if model == nil {
                let vm = DiscoverViewModel(bridge: appState.bridge, appState: appState)
                model = vm
                await vm.discover()
            }
        }
    }

    @ViewBuilder
    private func content(_ model: DiscoverViewModel) -> some View {
        @Bindable var model = model
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Add a device")
                    .font(.title2.weight(.semibold))
                Spacer()
                Button(model.pairPhase.succeeded ? "Done" : "Close") {
                    dismiss()
                }
                .keyboardShortcut(.cancelAction)
            }

            if model.pairPhase != .idle {
                PairProgressView(model: model)
            } else {
                discoverySection(model)
                Divider()
                explicitIPSection(model)
            }

            Spacer(minLength: 0)
        }
        .padding(20)
    }

    @ViewBuilder
    private func discoverySection(_ model: DiscoverViewModel) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("On this network", systemImage: "bonjour")
                    .font(.headline)
                Spacer()
                if model.isDiscovering {
                    ProgressView()
                        .controlSize(.small)
                        .padding(.trailing, 4)
                }
                Button(action: { Task { await model.discover() } }) {
                    Text(model.didDiscover ? "Discover again" : "Discover")
                }
                .disabled(model.isDiscovering)
            }
            if let error = model.discoverError {
                ErrorBanner(model: error)
            } else if model.discovered.isEmpty {
                Text(
                    model.didDiscover
                        ? "no devices discovered via mDNS; connect by IP instead below"
                        : "searching for SteamOS devkits on the local network…"
                )
                .font(.callout)
                .foregroundStyle(.secondary)
            } else {
                ForEach(model.discovered, id: \.address) { device in
                    HStack(spacing: 8) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text(device.name).font(.headline)
                                ViaBadge(via: device.via)
                            }
                            Text(
                                "\(device.address):\(String(device.port))"
                                    + " — login \(device.login ?? "?")"
                                    + " — sshd \(device.sshd == true ? "yes" : device.sshd == false ? "no" : "?")"
                            )
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Pair…") {
                            model.pair(target: device.name)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
    }

    @ViewBuilder
    private func explicitIPSection(_ model: DiscoverViewModel) -> some View {
        @Bindable var model = model
        VStack(alignment: .leading, spacing: 8) {
            Label("Add by IP", systemImage: "network")
                .font(.headline)
            HStack {
                TextField("192.0.2.10", text: $model.ipAddress)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 240)
                    .onSubmit {
                        if model.ipIsValid { model.pair(target: model.ipAddress) }
                    }
                Button("Pair…") {
                    model.pair(target: model.ipAddress)
                }
                .disabled(!model.ipIsValid)
            }
            if let error = model.ipValidationError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            Text("Pairing asks the device to approve this Mac; the request times out after two minutes.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

extension DiscoverViewModel.PairPhase {
    var succeeded: Bool {
        if case .succeeded = self { return true }
        return false
    }
}

struct DiscoverSheet_Previews: PreviewProvider {
    static var previews: some View {
        DiscoverSheet()
            .environment(AppState.preview(scenario: .populated))
    }
}
