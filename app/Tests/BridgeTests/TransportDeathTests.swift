import Foundation

// Transport death: pending requests fail with BridgeError.transport and the
// bridge transparently reconnects on the next call.

func testTransportDeathFailsPendingsAndReconnects() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let client = try await bridge.testingClient()

    // fake_slow is never answered before the server exits; fake_exit kills
    // the process. Both must fail with .transport.
    let slowTask = Task { try await client.callTool(name: "fake_slow") }
    try await Task.sleep(for: .milliseconds(200))  // let the slow request land
    let exitTask = Task { try await client.callTool(name: "fake_exit") }

    let slowError = try await expectThrows { try await slowTask.value }
    guard case .transport = try requireBridgeError(slowError) else {
        throw TestIssue("expected .transport for pending call, got \(slowError)",
                        file: #filePath, line: #line)
    }
    let exitError = try await expectThrows { try await exitTask.value }
    guard case .transport = try requireBridgeError(exitError) else {
        throw TestIssue("expected .transport for unanswered call, got \(exitError)",
                        file: #filePath, line: #line)
    }

    // The next call auto-connects a fresh server.
    let report = try await bridge.doctor()
    try expect(report.ok)

    // progressEvents stays open across reconnects: a fresh deploy still
    // delivers its 5 events.
    let start = try await bridge.deploy(
        DeploySpec(device: "fake-deck", gameid: "fake_game", sourceDir: "/tmp/payload"))
    let box = EventBox<ProgressNotification>()
    let consumer = collectAsync(
        bridge.progressEvents, matching: { $0.progressToken == start.progressToken }, into: box)
    defer { consumer.cancel() }
    let events = try await waitForCount(box, 5, timeout: 5)
    try expectEqual(events.map(\.progress), [1, 2, 3, 4, 5])
}
