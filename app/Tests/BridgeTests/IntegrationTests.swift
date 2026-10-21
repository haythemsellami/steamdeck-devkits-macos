import Foundation

// Real-server integration, strictly host-only (no discover/pair/status/deploy
// or anything that touches a device or the LAN). Runs by default via the
// `.dev(repoRoot:)` preset with an isolated DEVKIT_STATE_DIR.

func testRealServerHostOnly() async throws {
    let bridge = TestSupport.realBridge()
    defer { Task { await bridge.disconnect() } }

    // Handshake facts (recorded in app/Tests/Fixtures/initialize_result.json).
    let client = try await bridge.testingClient()
    let version = await client.serverProtocolVersion
    try expectEqual(version, "2025-06-18")
    let identity = await client.serverIdentity
    try expectEqual(identity?.name, "steamos-devkit")

    // Full tool surface.
    let names = try await client.listToolNames()
    try expectEqual(names.count, 33)

    // Host-only tool decodes.
    let report = try await bridge.doctor()
    try expectEqual(report.context, .mcpStdio)

    let devices = try await bridge.listDevices()
    _ = devices  // decodes; contents depend on the isolated state dir (empty)

    let capabilities = try await bridge.capabilities(device: nil)
    try expectEqual(capabilities.host.context, .mcpStdio)

    // isError mapping against real payloads (SDK-prefixed form).
    try await expectDevkitError(ErrorCodes.artifactNotFound) {
        try await bridge.readArtifact(artifactId: "art_01JFFFFFFFFFFFFFFFFFFFFFF0", tailLines: 5)
    }
    try await expectDevkitError(ErrorCodes.jobNotFound) {
        try await bridge.jobStatus(jobId: "job_01JFFFFFFFFFFFFFFFFFFFFFF0")
    }
}
