import SwiftUI

// DevicesView.swift — device registry views: the "All Devices" overview page
// and the view model behind registry loading, reachability probes, and
// forgetting devices. The sidebar rows themselves live in RootView.

@MainActor
@Observable
final class DevicesViewModel {
    let bridge: any DevkitBridge
    let appState: AppState

    /// Per-device probe currently in flight (disables duplicate probes).
    private(set) var probing: Set<String> = []

    init(bridge: any DevkitBridge, appState: AppState) {
        self.bridge = bridge
        self.appState = appState
    }

    func refresh() async {
        await appState.refreshDevices()
    }

    /// Fills the sidebar status dot for one device.
    func probe(_ device: Device) async {
        guard !probing.contains(device.name) else { return }
        probing.insert(device.name)
        defer { probing.remove(device.name) }
        do {
            _ = try await bridge.status(device: device.name)
            appState.markReachable(device.name, true)
        } catch {
            appState.markReachable(device.name, false)
        }
    }

    func probeAll() async {
        for device in appState.devices {
            await probe(device)
        }
    }

    func forget(_ device: Device) async {
        do {
            let result = try await bridge.forgetDevice(device.name)
            appState.presentInfo(result.message ?? "forgot \(device.name)")
            if appState.selection == .device(device.name) {
                appState.selection = .devices
            }
            await appState.refreshDevices()
        } catch {
            appState.present(error)
        }
    }
}

/// The `.devices` detail page when the registry is non-empty.
struct DevicesOverviewView: View {
    @Environment(AppState.self) private var appState
    @State private var model: DevicesViewModel?

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Loading devices…")
            }
        }
        .task {
            if model == nil {
                let vm = DevicesViewModel(bridge: appState.bridge, appState: appState)
                model = vm
                await vm.refresh()
                await vm.probeAll()
            }
        }
    }

    @ViewBuilder
    private func content(_ model: DevicesViewModel) -> some View {
        List {
            ForEach(appState.devices, id: \.name) { device in
                HStack(spacing: 10) {
                    ReachabilityDot(reachable: appState.reachability[device.name])
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(device.name).font(.headline)
                            FamilyBadge(family: device.family)
                        }
                        Text("\(device.address):\(String(device.port)) — login \(device.login)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text("paired \(Format.date(device.pairedAt)) — last seen \(Format.lastSeen(device.lastSeen))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if model.probing.contains(device.name) {
                        ProgressView().controlSize(.small)
                    }
                    Button("Open") {
                        appState.selection = .device(device.name)
                    }
                }
                .padding(.vertical, 4)
                .contextMenu {
                    Button("Open") { appState.selection = .device(device.name) }
                    Button("Probe reachability") {
                        Task { await model.probe(device) }
                    }
                    Divider()
                    Button("Forget Device…", role: .destructive) {
                        Task { await model.forget(device) }
                    }
                }
            }
        }
        .navigationTitle("Devices")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: { appState.showsDiscoverSheet = true }) {
                    Label("Add Device", systemImage: "plus")
                }
            }
            ToolbarItem(placement: .automatic) {
                Button(action: { Task { await model.refresh() } }) {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
            }
        }
    }
}

struct DevicesOverviewView_Previews: PreviewProvider {
    static var previews: some View {
        DevicesOverviewView()
            .environment(AppState.preview())
    }
}
