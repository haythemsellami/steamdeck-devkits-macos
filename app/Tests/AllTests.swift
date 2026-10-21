import Foundation

// Explicit test registry (no XCTest discovery on CLT). A second agent appends
// view-model tests to `viewModelTests` — keep it an editable array literal.
//
// nonisolated(unsafe): the arrays are immutable after initialization in
// practice (edited at compile time only); Swift 6 statics require Sendable
// otherwise.
enum AllTests {
    nonisolated(unsafe) static var bridgeTests: [(String, () async throws -> Void)] = [
        ("handshakeFacts", testHandshakeFacts),
        ("toolsListHas33Entries", testToolsListHas33Entries),
        ("outOfOrderResponses", testOutOfOrderResponses),
        ("malformedLineRecorded", testMalformedLineRecorded),
        ("unknownResponseIgnored", testUnknownResponseIgnored),
        ("unknownMethodRPCError", testUnknownMethodRPCError),
        ("requestCancellation", testRequestCancellation),
        ("fixtureDecodes", testFixtureDecodes),
        ("resultEnvelopeTools", testResultEnvelopeTools),
        ("emptyAndListWrapping", testEmptyAndListWrapping),
        ("progressSequence", testProgressSequence),
        ("jobStartersReturnTokens", testJobStartersReturnTokens),
        ("bareErrorPayload", testBareErrorPayload),
        ("prefixedErrorPayload", testPrefixedErrorPayload),
        ("readArtifactBogusID", testReadArtifactBogusID),
        ("jobStatusBogusID", testJobStatusBogusID),
        ("artifactDataBogusRPCError", testArtifactDataBogusRPCError),
        ("artifactDataBlob", testArtifactDataBlob),
        ("transportDeathFailsPendingsAndReconnects", testTransportDeathFailsPendingsAndReconnects),
        ("launchPresets", testLaunchPresets),
        ("fromEnvironmentOverride", testFromEnvironmentOverride),
        ("fromEnvironmentBundled", testFromEnvironmentBundled),
        ("realServerHostOnly", testRealServerHostOnly),
    ]

    // Hardware-gated flow (DEVKIT_TEST_DEVICE); skips pass when unset.
    // Driven by the integrator from a Terminal.app session (TCC grant) via
    // `scripts/test-swift.sh --hardware-only`.
    nonisolated(unsafe) static var hardwareTests: [(String, () async throws -> Void)] = [
        ("connectivity", testHardwareConnectivity),
        ("milestoneFlow", testHardwareMilestoneFlow),
    ]

    nonisolated(unsafe) static var viewModelTests: [(String, () async throws -> Void)] = [
        ("devicesLoadAndSelect", DevicesVMTests.devicesLoadAndSelect),
        ("pairSuccessAddsDevice", DevicesVMTests.pairSuccessAddsDevice),
        ("pairDeclinedSurfacesFriendlyError", DevicesVMTests.pairDeclinedSurfacesFriendlyError),
        ("pairCancelReturnsToIdle", DevicesVMTests.pairCancelReturnsToIdle),
        ("ipv4Validation", DevicesVMTests.ipv4Validation),
        ("gameIDValidation", DeployVMTests.gameIDValidation),
        ("checksumUpdateAreExclusive", DeployVMTests.checksumUpdateAreExclusive),
        ("specBuilding", DeployVMTests.specBuilding),
        ("deployProgressSequenceRouted", DeployVMTests.deployProgressSequenceRouted),
        ("mockDeployEmitsScriptedProgress", DeployVMTests.mockDeployEmitsScriptedProgress),
        ("devkitPayloadMapsCodeAndDetails", ErrorPresentationTests.devkitPayloadMapsCodeAndDetails),
        ("localNetworkPermissionHasRemediation", ErrorPresentationTests.localNetworkPermissionHasRemediation),
        ("pairingErrorsHaveFriendlyWording", ErrorPresentationTests.pairingErrorsHaveFriendlyWording),
        ("transportAndRpcErrorsMap", ErrorPresentationTests.transportAndRpcErrorsMap),
        ("cancellationIsNeverPresented", ErrorPresentationTests.cancellationIsNeverPresented),
        ("imageArtifactDecodesToNSImage", ArtifactsVMTests.imageArtifactDecodesToNSImage),
        ("textArtifactReadsExcerpt", ArtifactsVMTests.textArtifactReadsExcerpt),
        ("unknownSelectionFails", ArtifactsVMTests.unknownSelectionFails),
        ("missingPathClassified", ArtifactsVMTests.missingPathClassified),
        ("titlesLoadAndSelect", TitlesVMTests.titlesLoadAndSelect),
        ("launchShowsHonestReadiness", TitlesVMTests.launchShowsHonestReadiness),
        ("screenshotStartsJobAndNavigates", TitlesVMTests.screenshotStartsJobAndNavigates),
        ("deleteRequiresTypingTheGameID", TitlesVMTests.deleteRequiresTypingTheGameID),
        ("configFormMergesLeaveUnchanged", TitlesVMTests.configFormMergesLeaveUnchanged),
    ]
}
