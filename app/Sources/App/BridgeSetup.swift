import Foundation

// BridgeSetup.swift — the one place that decides which DevkitBridge the
// running app gets. Until the real MCP stdio client lands, the app runs on
// MockBridge so every screen stays testable end to end.
//
// Never fatalError here: the app must stay runnable during development.

enum BridgeSetup {
    static func makeProductionBridge() -> any DevkitBridge {
        // INTEGRATOR-REPLACE: MCPBridge landed in Bridge/Internal — swap to
        //   MCPBridge(launchConfig: BridgeLaunchConfig.fromEnvironment())
        // (or .dev(repoRoot:) / .bundled(resources:) per packaging), keeping
        // MockBridge as the no-server fallback. Verify Transport.swift is
        // settled (it was mid-edit by the bridge agent when this was written).
        MockBridge(scenario: .populated)
    }
}
