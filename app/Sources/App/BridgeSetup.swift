import Foundation

// BridgeSetup.swift — the one place that decides which DevkitBridge the
// running app gets. Until the real MCP stdio client lands, the app runs on
// MockBridge so every screen stays testable end to end.
//
// Never fatalError here: the app must stay runnable during development.

enum BridgeSetup {
    static func makeProductionBridge() -> any DevkitBridge {
        // INTEGRATOR-REPLACE: return MCPBridge(launch: BridgeLaunchConfig(...))
        // once Bridge/Internal ships; keep MockBridge as the fallback.
        MockBridge(scenario: .populated)
    }
}
