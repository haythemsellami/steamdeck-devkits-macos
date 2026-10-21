import Foundation

// Framing/session behavior against the Python fake server.

func requireBridgeError(
    _ error: Error, file: StaticString = #filePath, line: UInt = #line
) throws -> BridgeError {
    guard let bridgeError = error as? BridgeError else {
        throw TestIssue("expected BridgeError, got \(error)", file: file, line: line)
    }
    return bridgeError
}

/// The fake handshake mirrors the real server: protocolVersion "2025-06-18".
func testHandshakeFacts() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let client = try await bridge.testingClient()
    let version = await client.serverProtocolVersion
    let identity = await client.serverIdentity
    try expectEqual(version, "2025-06-18")
    try expectEqual(identity?.name, "fake-devkit")
}

/// tools/list from the fixture (captured from the real server) has 33 tools.
func testToolsListHas33Entries() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let names = try await bridge.testingClient().listToolNames()
    try expectEqual(names.count, 33)
    try expect(names.contains("devkit_deploy"))
    try expect(names.contains("devkit_job_status"))
}

/// The fake answers a "fake_slow" request only after the NEXT request —
/// responses arrive out of order and must route by id.
func testOutOfOrderResponses() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let client = try await bridge.testingClient()
    let slowTask = Task { try await client.callTool(name: "fake_slow") }
    try await Task.sleep(for: .milliseconds(200))  // let the slow request land first
    let fastOutcome = try await client.callTool(name: "devkit_doctor")
    let slowOutcome = try await slowTask.value
    try expect(!slowOutcome.isError)
    try expect(!fastOutcome.isError)
    // Distinct payloads prove correct id routing despite reversed order.
    try expectEqual(slowOutcome.structured?.objectValue?["ok"], .bool(true))
    try expectEqual(slowOutcome.structured?.objectValue?["message"]?.stringValue, "slow answer")
    try expect(fastOutcome.structured?.objectValue?["context"] != nil)
}

/// A malformed line from the server is counted and skipped; the pending
/// request still completes.
func testMalformedLineRecorded() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let client = try await bridge.testingClient()
    let before = await client.rpcDiagnostics().malformedLines
    let outcome = try await client.callTool(name: "fake_emit_garbage")
    try expect(!outcome.isError)
    let after = await client.rpcDiagnostics().malformedLines
    try expectEqual(after, before + 1)
}

/// A response with an unknown id is ignored (counted), never fatal.
func testUnknownResponseIgnored() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let client = try await bridge.testingClient()
    let before = await client.rpcDiagnostics().unknownResponses
    let outcome = try await client.callTool(name: "fake_emit_unknown_response")
    try expect(!outcome.isError)
    let after = await client.rpcDiagnostics().unknownResponses
    try expectEqual(after, before + 1)
}

/// Unknown methods get a JSON-RPC -32601 error, surfaced as BridgeError.rpc.
func testUnknownMethodRPCError() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let client = try await bridge.testingClient()
    let error = try await expectThrows { try await client.rawCall(method: "bogus/method", params: nil) }
    guard case .rpc(let code, _) = try requireBridgeError(error) else {
        throw TestIssue("expected .rpc, got \(error)", file: #filePath, line: #line)
    }
    try expectEqual(code, -32601)
}

/// Cancelling a pending call throws CancellationError for the caller and the
/// session stays usable (the server gets notifications/cancelled).
func testRequestCancellation() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let client = try await bridge.testingClient()
    let slowTask = Task { try await client.callTool(name: "fake_slow") }
    try await Task.sleep(for: .milliseconds(200))  // let the request land
    slowTask.cancel()
    let error = try await expectThrows { try await slowTask.value }
    try expect(error is CancellationError, "expected CancellationError, got \(error)")
    // The session survived: a normal call still works.
    let outcome = try await client.callTool(name: "devkit_doctor")
    try expect(!outcome.isError)
}
