import Foundation

// Launch-config presets and environment resolution.

func testLaunchPresets() throws {
    let dev = BridgeLaunchConfig.dev(repoRoot: "/repo")
    try expectEqual(dev.executable, "uv")
    try expectEqual(dev.arguments, ["run", "devkit-mcp"])
    try expectEqual(dev.workingDirectory, "/repo")
    try expectEqual(dev.environment, [:])

    let bundled = BridgeLaunchConfig.bundled(resources: "/R")
    try expectEqual(bundled.executable, "/R/python/bin/python3")
    try expectEqual(
        bundled.arguments, ["-c", "from steamos_devkit.cli.mcp import run; run()"])
    try expectEqual(bundled.environment["PYTHONPATH"], "/R/python-packages")
    try expectEqual(bundled.environment["DEVKIT_RSYNC"], "/R/bin/rsync")
}

func testFromEnvironmentOverride() throws {
    // A real directory without the embedded python: Bundle(url:) needs an
    // existing path, and the bundled preset must not trigger.
    let emptyDir = NSTemporaryDirectory() + "devkit-nobundle-" + UUID().uuidString
    try FileManager.default.createDirectory(atPath: emptyDir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(atPath: emptyDir) }
    let noBundle = try syntheticBundle(emptyDir)

    let override = BridgeLaunchConfig.fromEnvironment(
        bundle: noBundle,
        environment: ["DEVKIT_MCP_COMMAND": "/bin/echo hello world"])
    try expectEqual(override.executable, "/bin/echo")
    try expectEqual(override.arguments, ["hello", "world"])

    // No override, no bundled resources: dev preset rooted at DEVKIT_REPO_ROOT.
    let dev = BridgeLaunchConfig.fromEnvironment(
        bundle: noBundle,
        environment: ["DEVKIT_REPO_ROOT": "/x/repo"])
    try expectEqual(dev.executable, "uv")
    try expectEqual(dev.workingDirectory, "/x/repo")
}

private func syntheticBundle(_ path: String) throws -> Bundle {
    guard let bundle = Bundle(url: URL(fileURLWithPath: path)) else {
        throw TestIssue("Bundle(url:) rejected \(path)", file: #filePath, line: #line)
    }
    return bundle
}

/// The bundled preset wins when the app's resources contain the embedded
/// Python (packaging contract paths).
func testFromEnvironmentBundled() throws {
    let bundleDir = NSTemporaryDirectory() + "devkit-bundle-test-" + UUID().uuidString
    let resources = bundleDir + "/Contents/Resources"
    try FileManager.default.createDirectory(
        atPath: resources + "/python/bin", withIntermediateDirectories: true)
    FileManager.default.createFile(atPath: resources + "/python/bin/python3", contents: nil)
    defer { try? FileManager.default.removeItem(atPath: bundleDir) }

    let bundle = try syntheticBundle(bundleDir)
    let config = BridgeLaunchConfig.fromEnvironment(bundle: bundle, environment: [:])
    try expectEqual(config.executable, resources + "/python/bin/python3")
}
