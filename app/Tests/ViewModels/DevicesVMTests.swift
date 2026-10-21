import Foundation

// DevicesVMTests.swift — registry load + selection, reachability probe,
// pair success/failure surfacing, IPv4 validation.

enum DevicesVMTests {
    @MainActor
    static func devicesLoadAndSelect() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = DevicesViewModel(bridge: mock, appState: appState)

        await vm.refresh()
        try expectEqual(appState.devices.count, 2, "populated scenario has two devices")
        try expectEqual(appState.devices.first?.name, "steamdeck", "first device is the deck")

        appState.selection = .device("steamdeck")
        try expectEqual(appState.activeDeviceName, "steamdeck", "active device follows selection")

        await vm.probeAll()
        try expectEqual(appState.reachability["steamdeck"], true, "probe marks deck reachable")
        try expectEqual(appState.reachability["steamframe"], true, "probe marks frame reachable")
    }

    @MainActor
    static func pairSuccessAddsDevice() async throws {
        let mock = MockBridge(scenario: .empty)
        let appState = AppState(bridge: mock)
        let vm = DiscoverViewModel(bridge: mock, appState: appState)

        vm.pair(target: "192.0.2.10")
        try await waitFor("pair completes") { await vm.pairPhase != .inProgress }
        guard case .succeeded(let message) = vm.pairPhase else {
            throw VMTestFailure(message: "expected pairing success, got \(vm.pairPhase)")
        }
        try expectEqual(message, "paired", "success surfaces the result message")
        try expectEqual(appState.devices.count, 1, "paired device lands in the registry")
        try expectEqual(appState.devices.first?.name, "192.0.2.10", "explicit-IP pair keyed by address")
    }

    @MainActor
    static func pairDeclinedSurfacesFriendlyError() async throws {
        let mock = MockBridge(scenario: .pairingDeclined)
        let appState = AppState(bridge: mock)
        let vm = DiscoverViewModel(bridge: mock, appState: appState)

        vm.pair(target: "steamdeck")
        try await waitFor("pair fails") { await vm.pairPhase != .inProgress }
        guard case .failed(let banner) = vm.pairPhase else {
            throw VMTestFailure(message: "expected pairing failure, got \(vm.pairPhase)")
        }
        try expectEqual(banner.code, ErrorCodes.pairingDeclined, "declined code surfaces")
        try expect(
            banner.guidance?.contains("approve") == true,
            "declined banner carries approve-on-device guidance")
        try expectEqual(appState.devices.count, 0, "declined pairing adds no device")
    }

    @MainActor
    static func pairCancelReturnsToIdle() async throws {
        let mock = MockBridge(scenario: .populated)
        let appState = AppState(bridge: mock)
        let vm = DiscoverViewModel(bridge: mock, appState: appState)
        vm.pair(target: "steamdeck")
        try expectEqual(vm.pairPhase, .inProgress, "pairing starts in progress")
        vm.cancelPair()
        try expectEqual(vm.pairPhase, .idle, "cancel returns to idle")
    }

    static func ipv4Validation() throws {
        try expectNil(Validation.ipv4Error("192.0.2.10"), "plain IPv4 is valid")
        try expectNil(Validation.ipv4Error("0.0.0.0"), "zero IPv4 is valid")
        try expectNotNil(Validation.ipv4Error("192.0.2"), "three octets rejected")
        try expectNotNil(Validation.ipv4Error("192.0.2.300"), "octet > 255 rejected")
        try expectNotNil(Validation.ipv4Error("192.0.2.x"), "non-numeric octet rejected")
        try expectNotNil(Validation.ipv4Error(""), "empty rejected")
    }
}
