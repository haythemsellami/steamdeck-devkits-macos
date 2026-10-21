import AppKit
import Foundation

// ArtifactsVMTests.swift — list, select → preview state, image bytes →
// NSImage, text excerpt, file-state classification.

enum ArtifactsVMTests {
    @MainActor
    static func imageArtifactDecodesToNSImage() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = ArtifactsViewModel(bridge: mock, appState: appState)

        await vm.refresh()
        try expectEqual(vm.entries.count, 2, "populated scenario has two artifacts")

        await vm.select("art-shot-1")
        guard case .image(let data) = vm.preview else {
            throw VMTestFailure(message: "screenshot selects into an image preview, got \(vm.preview)")
        }
        let image = vm.makeImage(data)
        try expectNotNil(image, "screenshot bytes decode into NSImage")
        try expectEqual(image?.size.width, 8, "mock PNG is 8×8")
        try expectEqual(image?.size.height, 8, "mock PNG is 8×8")
    }

    @MainActor
    static func textArtifactReadsExcerpt() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = ArtifactsViewModel(bridge: mock, appState: appState)

        await vm.refresh()
        await vm.select("art-logs-1")
        guard case .text(let excerpt) = vm.preview else {
            throw VMTestFailure(message: "logs select into a text preview, got \(vm.preview)")
        }
        try expect(excerpt.contains("heartbeat"), "excerpt contains the canned log lines")
    }

    @MainActor
    static func unknownSelectionFails() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = ArtifactsViewModel(bridge: mock, appState: appState)
        await vm.refresh()
        await vm.select("art-does-not-exist")
        try expectEqual(vm.preview, .idle, "unknown id leaves the preview idle")
    }

    @MainActor
    static func missingPathClassified() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = ArtifactsViewModel(bridge: mock, appState: appState)
        await vm.refresh()
        guard let entry = vm.entries.first else {
            throw VMTestFailure(message: "at least one artifact")
        }
        // Mock artifacts point at /tmp/mock-devkit paths that don't exist.
        try expectEqual(vm.fileState(of: entry), .missing, "absent paths classify as missing")
    }
}
