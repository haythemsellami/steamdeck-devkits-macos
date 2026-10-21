import SwiftUI

// SteamOSDevkitApp.swift — app entry. One window ("SteamOS Devkit"),
// dark+light by default; AppState is created here and injected.
// The bridge comes from BridgeSetup (MockBridge until the integrator swaps
// in MCPBridge).

@MainActor
@main
struct SteamOSDevkitApp: App {
    @State private var appState = AppState.makeForProduction()

    var body: some Scene {
        WindowGroup("SteamOS Devkit") {
            RootView()
                .environment(appState)
                .frame(minWidth: 1100, minHeight: 700)
        }
        .windowResizability(.contentMinSize)
    }
}
