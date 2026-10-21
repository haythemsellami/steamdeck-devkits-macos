import Foundation

// Every fixture decodes through MCPBridge against the fake server.

/// DTO fixtures: doctor, devices, capabilities, status, titles, detail,
/// preview, launch, jobs, artifacts, readArtifact.
func testFixtureDecodes() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }

    let report = try await bridge.doctor()
    try expect(report.ok)
    try expectEqual(report.context, .mcpStdio)
    try expect(!report.checks.isEmpty)

    let devices = try await bridge.listDevices()
    try expectEqual(devices.count, 0)  // captured against an empty registry

    let capabilities = try await bridge.capabilities(device: nil)
    try expectEqual(capabilities.host.context, .mcpStdio)
    try expect(capabilities.device == nil)

    let status = try await bridge.status(device: "fake-deck")
    try expectEqual(status.hostname, "steamdeck-fake")
    try expectEqual(status.steamStatus, .os)
    try expectEqual(status.steamOsclientVersion, .string("1750000000"))
    try expectEqual(status.raw["numeric_extra"], .int(42))

    let titles = try await bridge.listTitles(device: "fake-deck")
    try expectEqual(titles.count, 2)
    try expectEqual(titles[0].gameid, "fake_game")
    try expectEqual(titles[1].registeredWithSteam, nil)

    let detail = try await bridge.inspectTitle(device: "fake-deck", gameid: "fake_game")
    try expectEqual(detail.config.entrypoint, "run.sh")
    try expectEqual(detail.config.runtime, .protonExperimental)
    try expectEqual(detail.config.env["DXVK_HUD"], "fps")
    try expectEqual(detail.config.settings["steam_play"], "1")
    try expectEqual(detail.remoteDir, "/home/deck/devkit-game/fake_game")

    let preview = try await bridge.previewDeploy(
        device: "fake-deck", gameid: "fake_game", sourceDir: "/tmp/payload",
        filters: FilterSpec(mode: .exclude, patterns: ["*.log"]), clean: false)
    try expectEqual(preview.filesAdded, 3)
    try expectEqual(preview.bytesToTransfer, 54321)

    let launch = try await bridge.launchTitle(device: "fake-deck", gameid: "fake_game")
    try expect(launch.launched)
    try expectEqual(launch.readiness, "unknown")

    let running = try await bridge.jobStatus(jobId: "job_01J9ZE0000000000000000000A")
    try expectEqual(running.state, .running)
    try expectEqual(running.kind, .deploy)
    try expectEqual(running.progress?.bytesDone, 512)

    let succeeded = try await bridge.jobStatus(jobId: "job_01J9ZE0000000000000000000B")
    try expectEqual(succeeded.state, .succeeded)
    try expectEqual(succeeded.artifacts, ["art_01J9ZE0000000000000000000C"])
    try expect(succeeded.finishedAt != nil)

    let jobs = try await bridge.listJobs(device: nil, state: nil)
    try expectEqual(jobs.count, 2)

    let artifacts = try await bridge.listArtifacts(device: nil, kind: nil)
    try expectEqual(artifacts.count, 1)
    try expectEqual(artifacts[0].artifact.kind, .screenshot)
    try expectEqual(artifacts[0].artifact.bytes, 69)
    try expectEqual(
        artifacts[0].resourceUri, "devkit://artifact/art_01J9ZE0000000000000000000C")

    let read = try await bridge.readArtifact(
        artifactId: "art_01J9ZE0000000000000000000C", tailLines: 50)
    try expectEqual(read.kind, "screenshot")
    try expectEqual(read.excerpt, nil)
}

/// The Result-envelope tools decode DevkitResult; deleteTitle always sends
/// confirm: true; multi-argument tools smoke through.
func testResultEnvelopeTools() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }

    let deleted = try await bridge.deleteTitle(device: "fake-deck", gameid: "fake_game")
    try expect(deleted.ok)

    let overlay = try await bridge.perfOverlay(device: "fake-deck", enabled: true)
    try expect(overlay.ok)

    let steamMode = try await bridge.setSteamMode(
        device: "fake-deck", mode: .os, gameid: nil, args: "-console", gdbserver: false)
    try expect(steamMode.ok)

    let configured = try await bridge.configureTitle(
        device: "fake-deck", gameid: "fake_game",
        config: TitleConfig(entrypoint: "run.sh", env: ["DXVK_HUD": "fps"]))
    try expect(configured.ok)

    let forgotten = try await bridge.forgetDevice("old-deck")
    try expect(forgotten.ok)
}

/// Empty collections decode through the {"result": [...]} structured wrapper.
func testEmptyAndListWrapping() async throws {
    let bridge = TestSupport.fakeBridge()
    defer { Task { await bridge.disconnect() } }
    let discovered = try await bridge.discover(timeoutSeconds: 1)
    try expectEqual(discovered.count, 0)
    let devices = try await bridge.listDevices()
    try expectEqual(devices.count, 0)
}
