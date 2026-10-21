import SwiftUI

// RootView.swift — single-window NavigationSplitView shell. Sidebar: devices,
// activity (jobs/artifacts), system (doctor). Detail renders per
// SidebarItem; the global banner sits above the detail pane. Sheets:
// discover/pair and deploy.

struct RootView: View {
    @Environment(AppState.self) private var appState

    var body: some View {
        @Bindable var appState = appState
        NavigationSplitView {
            List(selection: $appState.selection) {
                Section("Devices") {
                    Label("All Devices", systemImage: "rectangle.on.rectangle")
                        .tag(SidebarItem.devices)
                    ForEach(appState.devices, id: \.name) { device in
                        HStack(spacing: 8) {
                            ReachabilityDot(reachable: appState.reachability[device.name])
                            VStack(alignment: .leading, spacing: 1) {
                                Text(device.name)
                                Text("last seen \(Format.lastSeen(device.lastSeen))")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            FamilyBadge(family: device.family)
                        }
                        .tag(SidebarItem.device(device.name))
                    }
                }
                Section("Activity") {
                    Label("Jobs", systemImage: "list.bullet.rectangle")
                        .tag(SidebarItem.jobs)
                    Label("Artifacts", systemImage: "tray.full")
                        .tag(SidebarItem.artifacts)
                }
                Section("System") {
                    Label("Doctor", systemImage: "stethoscope")
                        .tag(SidebarItem.doctor)
                }
            }
            .listStyle(.sidebar)
            .navigationTitle("SteamOS Devkit")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button(action: { appState.showsDiscoverSheet = true }) {
                        Label("Add Device", systemImage: "plus")
                    }
                    .help("Discover devices on the local network, or add one by IP")
                }
            }
        } detail: {
            VStack(spacing: 0) {
                if let banner = appState.banner {
                    ErrorBanner(model: banner, onDismiss: appState.dismissBanner)
                        .padding([.horizontal, .top])
                }
                detail
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .sheet(isPresented: $appState.showsDiscoverSheet) {
            DiscoverSheet()
        }
        .sheet(isPresented: $appState.showsDeploySheet) {
            DeploySheet(device: appState.activeDeviceName)
        }
        .task {
            appState.start()
            await appState.refreshDevices()
        }
        .refreshable {
            await appState.refreshDevices()
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch appState.selection ?? .devices {
        case .devices:
            if appState.devices.isEmpty {
                OnboardingView()
            } else {
                DevicesOverviewView()
            }
        case .device(let name):
            DeviceDetailView(deviceName: name, initialTab: .overview)
        case .titles(let name):
            DeviceDetailView(deviceName: name, initialTab: .titles)
        case .jobs:
            JobsView()
        case .job(let jobID):
            JobDetailView(jobID: jobID)
        case .artifacts:
            ArtifactsView()
        case .doctor:
            DoctorView()
        }
    }
}

struct RootView_Previews: PreviewProvider {
    static var previews: some View {
        RootView()
            .environment(AppState.preview())
    }
}
