import Foundation

// Progress bridging and the job-start flow against the fake server.

/// devkit_deploy with a progress token: the fake emits 3 notifications BEFORE
/// the result and 2 AFTER. All 5 must land in progressEvents with the token
/// from the returned JobStart, in order. (AsyncStream buffers unconsumed
/// events, so the 3 pre-response notifications are still delivered to a
/// consumer that starts after deploy() returns.)
func testProgressSequence() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }

    let start = try await bridge.deploy(
        DeploySpec(device: "fake-deck", gameid: "fake_game", sourceDir: "/tmp/payload"))
    try expectEqual(start.jobId, "job_01J9ZE0000000000000000000A")
    try expect(start.progressToken.hasPrefix("gui-"))

    let events = try await collectProgressEvents(
        from: bridge, token: start.progressToken, count: 5, timeout: 5)
    try expectEqual(events.map(\.progress), [1, 2, 3, 4, 5])
    try expectEqual(events.map(\.total), [5, 5, 5, 5, 5])
    try expectEqual(events.first?.message, "phase-1")
    try expect(events.allSatisfy { $0.progressToken == start.progressToken })
}

/// Other job starters also return JobStart with a fresh token.
func testJobStartersReturnTokens() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let logs = try await bridge.fetchLogs(device: "fake-deck", kinds: [.steam, .proton])
    try expectEqual(logs.jobId, "job_01J9ZE0000000000000000000A")
    let shot = try await bridge.screenshot(device: "fake-deck", mode: .fullComposition)
    try expect(!shot.progressToken.isEmpty)
    let gpu = try await bridge.gpuTrace(device: "fake-deck")
    try expect(!gpu.jobId.isEmpty)
    let rgp = try await bridge.rgpCapture(device: "fake-deck")
    try expect(!rgp.jobId.isEmpty)
    let controller = try await bridge.controllerConfig(device: "fake-deck", gameid: "fake_game")
    try expect(!controller.jobId.isEmpty)
}

private func collectProgressEvents(
    from bridge: MCPBridge, token: String, count: Int, timeout: TimeInterval
) async throws -> [ProgressNotification] {
    let box = EventBox<ProgressNotification>()
    let consumer = collectAsync(bridge.progressEvents, matching: { $0.progressToken == token }, into: box)
    defer { consumer.cancel() }
    return try await waitForCount(box, count, timeout: timeout)
}
