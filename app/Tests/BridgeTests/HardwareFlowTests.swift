import Foundation

// Hardware-gated end-to-end flow: the GUI's exact data path (Swift MCPBridge
// ↔ real devkit-mcp over stdio ↔ a real Steam Deck), mirroring the proven
// 10-step flow of scripts/mcp_smoke.py (minus pair — the device is already
// paired).
//
// These tests run ONLY when DEVKIT_TEST_DEVICE is set (registry name or IP of
// a paired device); otherwise each prints SKIP and passes. They must be driven
// from a Terminal.app session (Local Network TCC grant): shells under tmux
// fail LAN contact with errno 65. Host-side state is the REAL default state
// dir — the paired-device registry lives there, so DEVKIT_STATE_DIR is
// deliberately not overridden here.
//
// Env vars:
//   DEVKIT_TEST_DEVICE   registry name or IP of a paired device (required)
//   DEVKIT_MCP_COMMAND   optional full command override for the server
//                        (honored via BridgeLaunchConfig.fromEnvironment);
//                        default is the dev preset at the repo root.
//   DEVKIT_REPO_ROOT     optional repo-root override for the dev preset.

private func hardwareSelector() -> String? {
    guard let selector = ProcessInfo.processInfo.environment["DEVKIT_TEST_DEVICE"],
          !selector.isEmpty else { return nil }
    return selector
}

/// fromEnvironment() honors DEVKIT_MCP_COMMAND; otherwise the dev preset at
/// the compile-time repo root (robust regardless of the caller's cwd).
private func makeHardwareBridge() async -> MCPBridge {
    let config: BridgeLaunchConfig
    if let command = ProcessInfo.processInfo.environment["DEVKIT_MCP_COMMAND"],
       !command.isEmpty {
        config = .fromEnvironment()
    } else {
        config = .dev(repoRoot: TestSupport.repoRoot)
    }
    let bridge = MCPBridge(launchConfig: config)
    await bridge.setCallTimeout(180)
    return bridge
}

private func requireHardwareDevice(
    _ bridge: MCPBridge, selector: String,
    file: StaticString = #filePath, line: UInt = #line
) async throws -> Device {
    let devices = try await bridge.listDevices()
    try expect(!devices.isEmpty, "no registered devices on this host", file: file, line: line)
    guard let device = devices.first(where: { $0.name == selector || $0.address == selector })
    else {
        throw TestIssue(
            "DEVKIT_TEST_DEVICE \(selector) matches no registry entry "
                + "(have: \(devices.map(\.name).joined(separator: ", ")))",
            file: file, line: line)
    }
    return device
}

private func pollJobTerminal(
    _ bridge: MCPBridge, jobId: String, deadlineSeconds: TimeInterval,
    file: StaticString = #filePath, line: UInt = #line
) async throws -> JobInfo {
    let deadline = Date().addingTimeInterval(deadlineSeconds)
    while true {
        let info = try await bridge.jobStatus(jobId: jobId)
        if info.state.isTerminal { return info }
        if Date() > deadline {
            throw TestIssue(
                "job \(jobId) not terminal within \(Int(deadlineSeconds))s "
                    + "(state \(info.state.rawValue), code \(info.code ?? "-"), "
                    + "message \(info.message ?? "-"))",
                file: file, line: line)
        }
        try await Task.sleep(for: .seconds(2))
    }
}

/// Snapshot when the box first holds >= count elements, or whatever it holds
/// at the deadline (the caller asserts).
private func waitForAtLeast<T: Sendable>(
    _ box: EventBox<T>, _ count: Int, timeout: TimeInterval
) async throws -> [T] {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        let snapshot = await box.snapshot()
        if snapshot.count >= count { return snapshot }
        try await Task.sleep(for: .milliseconds(250))
    }
    return await box.snapshot()
}

// MARK: - Tests

/// Steps 1-2: registry lookup + device status. Fast preflight for the flow.
func testHardwareConnectivity() async throws {
    guard let selector = hardwareSelector() else {
        print("SKIP hardware/connectivity (DEVKIT_TEST_DEVICE unset)")
        return
    }
    let bridge = await makeHardwareBridge()
    defer { Task { await bridge.disconnect() } }
    let device = try await requireHardwareDevice(bridge, selector: selector)
    let status = try await bridge.status(device: device.name)
    print(
        "EVIDENCE status hostname=\(status.hostname) os=\(status.osVersion) "
            + "steam=\(status.steamStatus.rawValue)")
}

/// Steps 3-10: payload → configure → preview → deploy (progress + terminal
/// job) → launch → fetch logs (artifact listed + read) → screenshot (PNG
/// bytes via resources/read) → delete cleanup (also on failure).
func testHardwareMilestoneFlow() async throws {
    guard let selector = hardwareSelector() else {
        print("SKIP hardware/milestoneFlow (DEVKIT_TEST_DEVICE unset)")
        return
    }
    let bridge = await makeHardwareBridge()
    defer { Task { await bridge.disconnect() } }
    let device = try await requireHardwareDevice(bridge, selector: selector)
    let deviceName = device.name

    // (2) status — first contact pays the per-process ensure_utils upload.
    let status = try await bridge.status(device: deviceName)
    print(
        "EVIDENCE status hostname=\(status.hostname) os=\(status.osVersion) "
            + "steam=\(status.steamStatus.rawValue)")

    // (3) tiny payload in a fresh temp dir; gameid matches GAMEID_REGEX.
    let gameid = "devkitguitest_\(ProcessInfo.processInfo.processIdentifier)"
    let payloadDir = NSTemporaryDirectory() + "devkit-gui-hw-" + UUID().uuidString
    let fileManager = FileManager.default
    try fileManager.createDirectory(atPath: payloadDir, withIntermediateDirectories: true)
    defer { try? fileManager.removeItem(atPath: payloadDir) }
    try "#!/bin/sh\necho hello from devkit-gui\n".write(
        toFile: payloadDir + "/hello.sh", atomically: true, encoding: .utf8)
    try fileManager.setAttributes(
        [.posixPermissions: 0o755], ofItemAtPath: payloadDir + "/hello.sh")
    try "payload\n".write(
        toFile: payloadDir + "/data.txt", atomically: true, encoding: .utf8)

    var titleDeployed = false
    do {
        // (4) configure the launch entrypoint + one env var.
        let configured = try await bridge.configureTitle(
            device: deviceName, gameid: gameid,
            config: TitleConfig(entrypoint: "hello.sh", env: ["DEVKIT_GUI_TEST": "1"]))
        try expect(configured.ok, "configureTitle not ok: \(configured.message ?? "-")")

        // (5) dry-run: everything is new.
        let preview = try await bridge.previewDeploy(
            device: deviceName, gameid: gameid, sourceDir: payloadDir,
            filters: nil, clean: false)
        try expect(preview.filesAdded > 0, "expected filesAdded > 0, got \(preview.filesAdded)")
        print("EVIDENCE preview filesAdded=\(preview.filesAdded) bytes=\(preview.bytesToTransfer)")

        // (6) deploy; collect progress for our token while polling the job.
        let start = try await bridge.deploy(
            DeploySpec(
                device: deviceName, gameid: gameid, sourceDir: payloadDir,
                register: true, launch: false))
        titleDeployed = true
        let box = EventBox<ProgressNotification>()
        let consumer = collectAsync(
            bridge.progressEvents, matching: { $0.progressToken == start.progressToken }, into: box)
        defer { consumer.cancel() }
        let deployInfo = try await pollJobTerminal(bridge, jobId: start.jobId, deadlineSeconds: 180)
        try expectEqual(
            deployInfo.state, JobState.succeeded,
            "deploy failed: \(deployInfo.code ?? "-") \(deployInfo.message ?? "-")")
        let events = try await waitForAtLeast(box, 1, timeout: 60)
        try expect(!events.isEmpty, "no progress notifications for token \(start.progressToken)")
        print(
            "EVIDENCE deploy job=\(start.jobId) artifacts=\(deployInfo.artifacts.count) "
                + "progressEvents=\(events.count)")

        // (7) launch; readiness is honestly "unknown" (no upstream signal).
        let launch = try await bridge.launchTitle(device: deviceName, gameid: gameid)
        try expect(launch.launched, "launchTitle: launched == false")
        try expectEqual(launch.readiness, "unknown")
        print("EVIDENCE launch launched=\(launch.launched) readiness=\(launch.readiness)")

        // (8) steam logs as an artifact: terminal job, listed, readable.
        let logs = try await bridge.fetchLogs(device: deviceName, kinds: [.steam])
        let logsInfo = try await pollJobTerminal(bridge, jobId: logs.jobId, deadlineSeconds: 300)
        try expectEqual(
            logsInfo.state, JobState.succeeded,
            "fetchLogs failed: \(logsInfo.code ?? "-") \(logsInfo.message ?? "-")")
        try expect(!logsInfo.artifacts.isEmpty, "fetchLogs produced no artifacts")
        let logArtifactId = logsInfo.artifacts[0]
        let entries = try await bridge.listArtifacts(device: deviceName, kind: nil)
        try expect(
            entries.contains { $0.artifact.id == logArtifactId },
            "listArtifacts(device:) missing \(logArtifactId)")
        let read = try await bridge.readArtifact(artifactId: logArtifactId, tailLines: 50)
        try expect(
            read.excerpt != nil || !read.resourceUri.isEmpty,
            "readArtifact: no excerpt and no resource link")
        print(
            "EVIDENCE fetchLogs artifact=\(logArtifactId) excerptBytes="
                + "\(read.excerpt?.utf8.count ?? 0)")

        // (9) screenshot: full composition, PNG bytes via resources/read.
        let shot = try await bridge.screenshot(device: deviceName, mode: .fullComposition)
        let shotInfo = try await pollJobTerminal(bridge, jobId: shot.jobId, deadlineSeconds: 120)
        try expectEqual(
            shotInfo.state, JobState.succeeded,
            "screenshot failed: \(shotInfo.code ?? "-") \(shotInfo.message ?? "-")")
        try expect(!shotInfo.artifacts.isEmpty, "screenshot produced no artifacts")
        let png = try await bridge.artifactData(artifactId: shotInfo.artifacts[0])
        try expectEqual(png.mimeType, "image/png")
        try expect(
            png.data.starts(with: [0x89, 0x50, 0x4E, 0x47]),
            "artifact payload is not a PNG (\(png.data.count) bytes)")
        print("EVIDENCE screenshot bytes=\(png.data.count) mime=\(png.mimeType)")
    } catch {
        // (10) cleanup on failure too: the device must not keep the title.
        if titleDeployed {
            _ = try? await bridge.deleteTitle(device: deviceName, gameid: gameid)
        }
        throw error
    }

    // (10) happy-path cleanup.
    let deleted = try await bridge.deleteTitle(device: deviceName, gameid: gameid)
    try expect(deleted.ok, "deleteTitle not ok: \(deleted.message ?? "-")")
    print("EVIDENCE cleanup deleted \(gameid)")
}
