import Foundation

// TitlesVMTests.swift — titles load, launch outcome wording, delete gating,
// and job-starting actions registering with AppState + navigating.

enum TitlesVMTests {
    @MainActor
    static func titlesLoadAndSelect() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = TitlesViewModel(bridge: mock, appState: appState, deviceName: "steamdeck")

        await vm.refresh()
        try expectEqual(vm.titles.count, 2, "deck has two titles")
        try expectEqual(vm.rows.map(\.id), ["zz_devkit_test", "proton_demo"], "rows keyed by gameid")

        await vm.select("zz_devkit_test")
        try expectEqual(vm.detail?.config.entrypoint, "./zz_devkit_test", "detail loads config")
        try expectEqual(vm.detail?.config.runtime, nil, "linux title has no compat tool")
    }

    @MainActor
    static func launchShowsHonestReadiness() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = TitlesViewModel(bridge: mock, appState: appState, deviceName: "steamdeck")

        await vm.launch("zz_devkit_test")
        guard let outcome = vm.actionOutcome else {
            throw VMTestFailure(message: "launch produces an outcome banner")
        }
        try expectEqual(outcome.kind, .info, "launch outcome is informational")
        try expect(
            outcome.message.contains("readiness unknown"),
            "launch banner is honest about missing readiness")
    }

    @MainActor
    static func screenshotStartsJobAndNavigates() async throws {
        let mock = MockBridge(scenario: .populated)
        mock.eventDelay = .milliseconds(10)
        let appState = AppState(bridge: mock)
        appState.start()
        let vm = TitlesViewModel(bridge: mock, appState: appState, deviceName: "steamdeck")

        await vm.screenshot()
        guard case .job(let jobID) = appState.selection else {
            throw VMTestFailure(message: "screenshot navigates to the new job")
        }
        try await waitFor("screenshot job succeeds", timeout: 10) {
            await appState.jobs[jobID]?.state == .succeeded
        }
        let artifacts = appState.jobs[jobID]?.artifacts ?? []
        try expectEqual(artifacts.count, 1, "screenshot job yields one artifact")
    }

    @MainActor
    static func deleteRequiresTypingTheGameID() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = TitlesViewModel(bridge: mock, appState: appState, deviceName: "steamdeck")
        await vm.refresh()

        let title: Title
        guard let found = vm.titles.first(where: { $0.gameid == "proton_demo" }) else {
            throw VMTestFailure(message: "proton_demo present")
        }
        title = found
        vm.beginDelete(title)
        try expect(!vm.deleteConfirmed, "empty confirmation does not confirm")
        vm.deleteConfirmationText = "proton"
        try expect(!vm.deleteConfirmed, "partial gameid does not confirm")
        vm.deleteConfirmationText = "proton_demo"
        try expect(vm.deleteConfirmed, "exact gameid confirms")

        await vm.confirmDelete()
        try expectEqual(vm.titles.count, 1, "title deleted from the list")
        try expectNil(vm.titlePendingDeletion, "delete sheet dismissed")
    }

    @MainActor
    static func configFormMergesLeaveUnchanged() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = TitleConfigFormModel(
            bridge: mock, appState: appState,
            deviceName: "steamdeck", gameid: "zz_devkit_test")

        await vm.load()
        try expect(vm.didLoad, "form prefilled from inspectTitle")
        try expectEqual(vm.draft.entrypoint, "./zz_devkit_test", "entrypoint prefilled")
        try expectEqual(vm.draft.parsedArgs, ["--fullscreen"], "args prefilled, one per line")

        // Clearing a collection must NOT wipe it server-side (H1 semantics):
        // the DTO carries an empty collection, and the mock leaves values.
        vm.draft.argsText = ""
        vm.draft.entrypoint = "./zz_devkit_test"
        try expectEqual(vm.draft.makeConfig().args, [], "empty args text → empty collection")
        await vm.save()
        guard case .some(.info) = vm.saveOutcome.map(\.kind) else {
            throw VMTestFailure(message: "save reports success")
        }
        let detail = try await mock.inspectTitle(device: "steamdeck", gameid: "zz_devkit_test")
        try expectEqual(
            detail.config.args, ["--fullscreen"],
            "empty args collection left the stored args unchanged")
    }
}
