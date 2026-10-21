import Foundation

// Launch presets for the `devkit-mcp` stdio server. The transport merges
// `environment` over a sanitized base (PATH, HOME, UV_*) — see Transport.swift.
extension BridgeLaunchConfig {
    /// In-repo development server: `uv run devkit-mcp` from the repo root.
    static func dev(repoRoot: String) -> BridgeLaunchConfig {
        BridgeLaunchConfig(
            executable: "uv",
            arguments: ["run", "devkit-mcp"],
            environment: [:],
            workingDirectory: repoRoot
        )
    }

    /// Packaged app: the embedded Python plus installed package tree.
    /// `resources` is the bundle's Resources directory; packaging installs
    /// exactly `python/bin/python3`, `python-packages/`, and `bin/rsync`.
    static func bundled(resources: String) -> BridgeLaunchConfig {
        BridgeLaunchConfig(
            executable: resources + "/python/bin/python3",
            arguments: ["-c", "from steamos_devkit.cli.mcp import run; run()"],
            environment: [
                "PYTHONPATH": resources + "/python-packages",
                "DEVKIT_RSYNC": resources + "/bin/rsync",
            ]
        )
    }

    /// Resolution order: `DEVKIT_MCP_COMMAND` (full command string, split on
    /// spaces — for tests and power users), then the bundled server when the
    /// app's resources contain the embedded Python, then the dev server rooted
    /// at `DEVKIT_REPO_ROOT` or the process's working directory.
    static func fromEnvironment(
        bundle: Bundle = .main,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> BridgeLaunchConfig {
        if let command = environment["DEVKIT_MCP_COMMAND"], !command.isEmpty {
            var parts = command.split(separator: " ").map(String.init)
            let executable = parts.removeFirst()
            return BridgeLaunchConfig(executable: executable, arguments: parts)
        }
        if let resources = bundle.resourceURL?.path,
           FileManager.default.fileExists(atPath: resources + "/python/bin/python3") {
            return .bundled(resources: resources)
        }
        return .dev(
            repoRoot: environment["DEVKIT_REPO_ROOT"]
                ?? FileManager.default.currentDirectoryPath
        )
    }
}
