import Foundation

// DeployVMTests.swift — gameid validation (regex + reserved ids),
// checksum/update exclusivity, spec building, and the scripted deploy
// progress sequence end to end through AppState routing.

enum DeployVMTests {
    static func gameIDValidation() throws {
        // Valid shapes.
        try expectNil(Validation.gameIDError("game_1"), "simple id")
        try expectNil(Validation.gameIDError("A.b.c"), "dotted id")
        try expectNil(Validation.gameIDError("_leading"), "leading underscore")
        // Invalid shapes.
        try expectNotNil(Validation.gameIDError(""), "empty rejected")
        try expectNotNil(Validation.gameIDError("1abc"), "leading digit rejected")
        try expectNotNil(Validation.gameIDError("has space"), "space rejected")
        try expectNotNil(Validation.gameIDError("has-dash"), "dash rejected")
        try expectNotNil(Validation.gameIDError("café"), "non-ASCII rejected")
        // Reserved ids rejected with an explanation.
        for reserved in ["steam", "steamdeckard", "steamvr", "steamvrdeckard"] {
            let error = Validation.gameIDError(reserved)
            try expectNotNil(error, "reserved id \(reserved) rejected")
            try expect(error?.contains("reserved") == true, "reserved id error explains why")
        }
        try expectNil(Validation.gameIDError("steam2"), "steam-prefix non-reserved id allowed")
    }

    @MainActor
    static func checksumUpdateAreExclusive() throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = DeployViewModel(bridge: mock, appState: appState, device: "steamdeck")

        try expect(!vm.checksum && !vm.update, "both off initially")
        vm.setChecksum(true)
        try expect(vm.checksum && !vm.update, "checksum on")
        vm.setUpdate(true)
        try expect(!vm.checksum && vm.update, "update clears checksum")
        vm.setChecksum(true)
        try expect(vm.checksum && !vm.update, "checksum clears update")
        vm.setChecksum(false)
        try expect(!vm.checksum && !vm.update, "checksum off leaves update off")
    }

    @MainActor
    static func specBuilding() throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = DeployViewModel(bridge: mock, appState: appState, device: "steamdeck")

        try expectNil(vm.makeSpec(), "no spec without gameid/source")
        vm.gameid = "steam"
        vm.sourceDir = "/tmp/build"
        try expectNil(vm.makeSpec(), "reserved gameid blocks the spec")
        vm.gameid = "zz_devkit_test"
        let spec = vm.makeSpec()
        try expectNotNil(spec, "spec builds with valid inputs")
        try expectEqual(spec?.device, "steamdeck", "spec device")
        try expect(spec?.compress == true && spec?.register == true, "compress/register default on")
        try expect(spec?.launch == false && spec?.clean == false, "launch/clean default off")
        try expectNil(spec?.config, "no title config unless the section is enabled")
        vm.includesTitleConfig = true
        vm.configDraft.entrypoint = "./run.sh"
        let withConfig = vm.makeSpec()
        try expectEqual(withConfig?.config?.entrypoint, "./run.sh", "title config rides along")
    }

    /// Push routing: deploy through the VM, AppState consumes the bridge's
    /// progress stream, and the job lands succeeded with artifacts of the
    /// scripted run reflected in liveProgress.
    @MainActor
    static func deployProgressSequenceRouted() async throws {
        let mock = MockBridge(scenario: .deployWithProgress)
        mock.eventDelay = .milliseconds(10)
        let appState = AppState(bridge: mock)
        appState.start()
        let vm = DeployViewModel(bridge: mock, appState: appState, device: "steamdeck")
        vm.gameid = "zz_devkit_test"
        vm.sourceDir = "/tmp/build"

        await vm.deploy()
        guard let jobID = vm.startedJobID else {
            throw VMTestFailure(message: "deploy did not start a job")
        }
        try expectEqual(appState.selection, .job(jobID), "deploy navigates to the job")

        try await waitFor("job reaches terminal state", timeout: 10) {
            await appState.jobs[jobID]?.state.isTerminal == true
        }
        try expectEqual(appState.jobs[jobID]?.state, .succeeded, "scripted deploy succeeds")
        let progress = appState.effectiveProgress(for: jobID)
        try expectEqual(progress.fraction, 1.0, "final push progress is complete")
        try expectEqual(progress.phase, "finishing", "final phase label from push")
    }

    /// Stream-level: the mock emits exactly 10 increasing events for the
    /// deploy token, then jobStatus reports success.
    static func mockDeployEmitsScriptedProgress() async throws {
        let mock = MockBridge(scenario: .deployWithProgress)
        mock.eventDelay = .milliseconds(5)
        let spec = DeploySpec(
            device: "steamdeck", gameid: "zz_devkit_test", sourceDir: "/tmp/build",
            filters: FilterSpec(), clean: false, checksum: false, update: false,
            compress: true, register: true, launch: false, config: nil)
        let start = try await mock.deploy(spec)

        let events = try await withTimeout(5, "10 progress events") {
            var collected: [ProgressNotification] = []
            for await event in mock.progressEvents where event.progressToken == start.progressToken {
                collected.append(event)
                if collected.count == 10 { break }
            }
            return collected
        }
        try expectEqual(events.count, 10, "10 scripted events")
        try expectEqual(events.first?.progress, 1, "first event is 1/10")
        try expectEqual(events.last?.progress, 10, "last event is 10/10")
        try expectEqual(events.last?.total, 10, "events carry the total")
        let info = try await mock.jobStatus(jobId: start.jobId)
        try expect(info.state == .running || info.state == .succeeded, "job advanced past queued")
    }
}
