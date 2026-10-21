import Foundation

// BridgeSetup.swift — the one place that decides which DevkitBridge the
// running app gets. Until the real MCP stdio client lands, the app runs on
// MockBridge so every screen stays testable end to end.
//
// Never fatalError here: the app must stay runnable during development.

enum BridgeSetup {
    static func makeProductionBridge() -> any DevkitBridge {
        // The production app always runs the real MCP stdio client; MockBridge
        // stays in previews and view-model tests. A server that cannot spawn
        // surfaces as a BridgeError through the normal banner — no silent
        // fake-data fallback.
        MCPBridge(launchConfig: .fromEnvironment())
    }
}
