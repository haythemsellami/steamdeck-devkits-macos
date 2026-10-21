import Foundation

/// Shared locations and launch configs for the bridge test suite.
enum TestSupport {
    /// Repo root: `DEVKIT_REPO_ROOT` wins; otherwise derived from this file's
    /// compile-time path (<repo>/app/Tests/TestSupport.swift).
    static let repoRoot: String = {
        if let override = ProcessInfo.processInfo.environment["DEVKIT_REPO_ROOT"],
           !override.isEmpty {
            return override
        }
        return URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // app/Tests
            .deletingLastPathComponent()  // app
            .deletingLastPathComponent()  // <repo>
            .path
    }()

    static let fakeServerPath = repoRoot + "/app/Tests/FakeServer/fake_mcp.py"

    /// Launch the Python fake server (stdlib only, so the CLT python3 works).
    static func fakeLaunchConfig() -> BridgeLaunchConfig {
        BridgeLaunchConfig(executable: "python3", arguments: [fakeServerPath])
    }

    static func fakeBridge() -> MCPBridge {
        MCPBridge(launchConfig: fakeLaunchConfig())
    }

    /// The real server via the dev preset, with an isolated state dir so
    /// host-only probes (doctor/capabilities) never touch real state.
    static func realBridge() -> MCPBridge {
        var config = BridgeLaunchConfig.dev(repoRoot: repoRoot)
        config.environment["DEVKIT_STATE_DIR"] =
            NSTemporaryDirectory() + "devkit-swift-tests-" + UUID().uuidString
        return MCPBridge(launchConfig: config)
    }

    /// The 1x1 red PNG the fake serves for `devkit://artifact/art_FAKE`
    /// (69 bytes; sha256 b1ff9c8e…4640).
    static let fakePNGBase64 =
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    static let fakePNGBytes = 69
}
