import Foundation

// isError mapping and resources/read against the fake server.

/// Bare payload JSON (as the brief's fake contract emits) maps to .devkit
/// with code, message, and details preserved.
func testBareErrorPayload() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let error = try await expectThrows { try await bridge.status(device: "boom") }
    guard case .devkit(let payload) = try requireBridgeError(error) else {
        throw TestIssue("expected .devkit, got \(error)", file: #filePath, line: #line)
    }
    try expectEqual(payload.code, ErrorCodes.deviceUnreachable)
    try expectEqual(payload.message, "cannot reach boom")
    try expectEqual(payload.details?["target"], .string("boom"))
}

/// The real server's SDK-prefixed form ("Error executing tool <name>: {json}")
/// maps to the same .devkit error.
func testPrefixedErrorPayload() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let error = try await expectThrows { try await bridge.status(device: "boom-prefix") }
    guard case .devkit(let payload) = try requireBridgeError(error) else {
        throw TestIssue("expected .devkit, got \(error)", file: #filePath, line: #line)
    }
    try expectEqual(payload.code, ErrorCodes.deviceUnreachable)
    try expectEqual(payload.message, "cannot reach boom-prefix")
}

func testReadArtifactBogusID() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    try await expectDevkitError(ErrorCodes.artifactNotFound) {
        try await bridge.readArtifact(artifactId: "art_01JFFFFFFFFFFFFFFFFFFFFFF0", tailLines: 50)
    }
}

func testJobStatusBogusID() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    try await expectDevkitError(ErrorCodes.jobNotFound) {
        try await bridge.jobStatus(jobId: "job_01JFFFFFFFFFFFFFFFFFFFFFF0")
    }
}

/// resources/read failures are JSON-RPC errors (-32602), not isError results.
func testArtifactDataBogusRPCError() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let error = try await expectThrows { try await bridge.artifactData(artifactId: "art_NOPE") }
    guard case .rpc(let code, let message) = try requireBridgeError(error) else {
        throw TestIssue("expected .rpc, got \(error)", file: #filePath, line: #line)
    }
    try expectEqual(code, -32602)
    try expect(message.contains("ARTIFACT_NOT_FOUND"), message)
}

/// The artifact blob decodes to the exact PNG bytes with its MIME type.
func testArtifactDataBlob() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let payload = try await bridge.artifactData(artifactId: "art_FAKE")
    try expectEqual(payload.mimeType, "image/png")
    try expectEqual(payload.data.count, TestSupport.fakePNGBytes)
    let expected = Data(base64Encoded: TestSupport.fakePNGBase64)!
    try expectEqual(payload.data, expected)
    try expect(payload.data.starts(with: [0x89, 0x50, 0x4E, 0x47]))  // \x89PNG
}
