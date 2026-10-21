import Foundation

/// The real stdio MCP client behind the `DevkitBridge` protocol: one method
/// per `devkit_*` tool (spec section 11), newline-delimited JSON-RPC to the
/// `devkit-mcp` server process.
///
/// Decoding (verified against the real server, mcp SDK 2.3.0, 2026-10-21):
/// - Success: `result.structuredContent` is authoritative; object payloads at
///   top level, array payloads wrapped as `{"result": [...]}` (unwrapped here
///   for collection decodes). Fallback: parse `content[0].text` as JSON (absent
///   for empty arrays, so structured content is required there — it is always
///   present on success).
/// - Tool error: `isError: true`, no structured content; `content[0].text` is
///   `"Error executing tool <name>: {"code":...}"` — the SDK prefixes the
///   payload. The prefix is stripped before decoding `DevkitErrorPayload`.
///
/// Connection policy: methods auto-connect (idempotent) so the UI never has
/// to sequence `connect()`; `disconnect()` is explicit. After an unexpected
/// server death the next call transparently reconnects. `progressEvents`
/// therefore stays open for the bridge's whole lifetime (never finished) —
/// a stream finished on server death could never deliver events from a
/// reconnected session.
final actor MCPBridge: DevkitBridge {
    static let defaultTimeout: TimeInterval = 60

    private let launchConfig: BridgeLaunchConfig
    private var client: MCPClient?
    private var connectTask: Task<MCPClient, Error>?

    /// Connection-level progress for every job started with a token. Open for
    /// the bridge's lifetime; consumers filter by `JobStart.progressToken`.
    nonisolated let progressEvents: AsyncStream<ProgressNotification>
    private let progressContinuation: AsyncStream<ProgressNotification>.Continuation

    init(launchConfig: BridgeLaunchConfig) {
        self.launchConfig = launchConfig
        var continuation: AsyncStream<ProgressNotification>.Continuation!
        self.progressEvents = AsyncStream { continuation = $0 }
        self.progressContinuation = continuation
    }

    func connect() async throws {
        _ = try await ensureConnected()
    }

    func disconnect() async {
        let stale = client
        client = nil
        await stale?.disconnect()
    }

    // MARK: - Discovery, registry, pairing, status

    func doctor() async throws -> DoctorReport {
        try await callTool(DoctorReport.self, "devkit_doctor")
    }

    func discover(timeoutSeconds: Int) async throws -> [DiscoveredDevice] {
        try await callTool(
            [DiscoveredDevice].self, "devkit_discover",
            args: ["timeout_s": .int(timeoutSeconds)],
            timeout: TimeInterval(timeoutSeconds) + 30
        )
    }

    func listDevices() async throws -> [Device] {
        try await callTool([Device].self, "devkit_list_devices")
    }

    func forgetDevice(_ device: String) async throws -> DevkitResult {
        try await callTool(DevkitResult.self, "devkit_forget_device", args: deviceArgs(device))
    }

    func pair(device: String, timeoutSeconds: Int) async throws -> DevkitResult {
        try await callTool(
            DevkitResult.self, "devkit_pair",
            args: ["device": .string(device), "timeout_s": .int(timeoutSeconds)],
            timeout: TimeInterval(timeoutSeconds) + 30
        )
    }

    func status(device: String) async throws -> DeviceStatus {
        try await callTool(DeviceStatus.self, "devkit_status", args: deviceArgs(device))
    }

    func capabilities(device: String?) async throws -> Capabilities {
        var args: [String: JSONValue] = [:]
        if let device { args["device"] = .string(device) }
        return try await callTool(Capabilities.self, "devkit_capabilities", args: args)
    }

    // MARK: - Titles and deploys

    func listTitles(device: String) async throws -> [Title] {
        try await callTool([Title].self, "devkit_list_titles", args: deviceArgs(device))
    }

    func inspectTitle(device: String, gameid: String) async throws -> TitleDetail {
        try await callTool(
            TitleDetail.self, "devkit_inspect_title", args: titleArgs(device, gameid))
    }

    func configureTitle(device: String, gameid: String, config: TitleConfig) async throws
        -> DevkitResult
    {
        var args = titleArgs(device, gameid)
        // Flattened TitleConfig fields; empty collections and nil entrypoint
        // mean "leave unchanged" (spec appendix A) and are omitted.
        if let entrypoint = config.entrypoint { args["entrypoint"] = .string(entrypoint) }
        if !config.args.isEmpty { args["args"] = .array(config.args.map { .string($0) }) }
        if !config.env.isEmpty { args["env"] = .object(config.env.mapValues { .string($0) }) }
        if let runtime = config.runtime { args["runtime"] = .string(runtime.rawValue) }
        if !config.settings.isEmpty {
            args["settings"] = .object(config.settings.mapValues { .string($0) })
        }
        return try await callTool(DevkitResult.self, "devkit_configure_title", args: args)
    }

    func previewDeploy(
        device: String, gameid: String, sourceDir: String,
        filters: FilterSpec?, clean: Bool
    ) async throws -> DeployPreview {
        var args = titleArgs(device, gameid)
        args["source_dir"] = .string(sourceDir)
        args["clean"] = .bool(clean)
        if let filters { args["filters"] = Self.filtersArgs(filters) }
        return try await callTool(DeployPreview.self, "devkit_preview_deploy", args: args)
    }

    func deploy(_ spec: DeploySpec) async throws -> JobStart {
        // Arguments are the spec's fields flattened at the top level (the
        // server builds its own DeploySpec from them).
        var args: [String: JSONValue] = [
            "device": .string(spec.device),
            "gameid": .string(spec.gameid),
            "source_dir": .string(spec.sourceDir),
            "filters": Self.filtersArgs(spec.filters),
            "clean": .bool(spec.clean),
            "checksum": .bool(spec.checksum),
            "update": .bool(spec.update),
            "compress": .bool(spec.compress),
            "register": .bool(spec.register),
            "launch": .bool(spec.launch),
        ]
        if let config = spec.config { args["config"] = Self.configValue(config) }
        return try await startJob("devkit_deploy", args: args)
    }

    func registerTitle(device: String, gameid: String) async throws -> DevkitResult {
        try await callTool(DevkitResult.self, "devkit_register_title", args: titleArgs(device, gameid))
    }

    func launchTitle(device: String, gameid: String) async throws -> LaunchResult {
        try await callTool(LaunchResult.self, "devkit_launch_title", args: titleArgs(device, gameid))
    }

    func deleteTitle(device: String, gameid: String) async throws -> DevkitResult {
        var args = titleArgs(device, gameid)
        args["confirm"] = .bool(true)  // the tool requires literal true
        return try await callTool(DevkitResult.self, "devkit_delete_title", args: args)
    }

    // MARK: - Diagnostics (jobs)

    func fetchLogs(device: String, kinds: [LogKind]) async throws -> JobStart {
        try await startJob(
            "devkit_fetch_logs",
            args: [
                "device": .string(device),
                "kinds": .array(kinds.map { .string($0.rawValue) }),
            ]
        )
    }

    func screenshot(device: String, mode: ScreenshotMode) async throws -> JobStart {
        try await startJob(
            "devkit_screenshot",
            args: ["device": .string(device), "mode": .int(mode.rawValue)]
        )
    }

    func controllerConfig(device: String, gameid: String) async throws -> JobStart {
        try await startJob("devkit_controller_config", args: titleArgs(device, gameid))
    }

    func gpuTrace(device: String) async throws -> JobStart {
        try await startJob("devkit_gpu_trace", args: deviceArgs(device))
    }

    func rgpCapture(device: String) async throws -> JobStart {
        try await startJob("devkit_rgp_capture", args: deviceArgs(device))
    }

    // MARK: - Profiling toggles, session

    func perfOverlay(device: String, enabled: Bool) async throws -> DevkitResult {
        try await callTool(DevkitResult.self, "devkit_perf_overlay", args: toggleArgs(device, enabled))
    }

    func frametimes(device: String, enabled: Bool) async throws -> DevkitResult {
        try await callTool(DevkitResult.self, "devkit_frametimes", args: toggleArgs(device, enabled))
    }

    func renderdocLayer(device: String, enabled: Bool) async throws -> DevkitResult {
        try await callTool(DevkitResult.self, "devkit_renderdoc_layer", args: toggleArgs(device, enabled))
    }

    func renderdocServer(device: String, action: RenderdocServerAction) async throws -> DevkitResult {
        try await callTool(
            DevkitResult.self, "devkit_renderdoc_server",
            args: ["device": .string(device), "action": .string(action.rawValue)]
        )
    }

    func protonLogging(device: String, mode: ProtonLogMode, winedebug: String?) async throws
        -> DevkitResult
    {
        var args: [String: JSONValue] = [
            "device": .string(device),
            "mode": .string(mode.rawValue),
        ]
        if let winedebug { args["winedebug"] = .string(winedebug) }
        return try await callTool(DevkitResult.self, "devkit_proton_logging", args: args)
    }

    func setSteamMode(
        device: String, mode: SteamMode, gameid: String?, args cliArgs: String?, gdbserver: Bool
    ) async throws -> DevkitResult {
        var args: [String: JSONValue] = [
            "device": .string(device),
            "mode": .string(mode.rawValue),
            "gdbserver": .bool(gdbserver),
        ]
        if let gameid { args["gameid"] = .string(gameid) }
        if let cliArgs { args["args"] = .string(cliArgs) }
        return try await callTool(DevkitResult.self, "devkit_set_steam_mode", args: args)
    }

    func setSession(device: String, session: DeviceSession) async throws -> DevkitResult {
        try await callTool(
            DevkitResult.self, "devkit_set_session",
            args: ["device": .string(device), "session": .string(session.rawValue)]
        )
    }

    func reloadSteam(device: String) async throws -> DevkitResult {
        try await callTool(DevkitResult.self, "devkit_reload_steam", args: deviceArgs(device))
    }

    // MARK: - Jobs and artifacts

    func jobStatus(jobId: String) async throws -> JobInfo {
        try await callTool(JobInfo.self, "devkit_job_status", args: ["job_id": .string(jobId)])
    }

    func jobCancel(jobId: String) async throws -> DevkitResult {
        try await callTool(DevkitResult.self, "devkit_job_cancel", args: ["job_id": .string(jobId)])
    }

    func listJobs(device: String?, state: JobState?) async throws -> [JobInfo] {
        var args: [String: JSONValue] = [:]
        if let device { args["device"] = .string(device) }
        if let state { args["state"] = .string(state.rawValue) }
        return try await callTool([JobInfo].self, "devkit_list_jobs", args: args)
    }

    func listArtifacts(device: String?, kind: ArtifactKind?) async throws -> [ArtifactEntry] {
        var args: [String: JSONValue] = [:]
        if let device { args["device"] = .string(device) }
        if let kind { args["kind"] = .string(kind.rawValue) }
        return try await callTool([ArtifactEntry].self, "devkit_list_artifacts", args: args)
    }

    func readArtifact(artifactId: String, tailLines: Int) async throws -> ArtifactRead {
        try await callTool(
            ArtifactRead.self, "devkit_read_artifact",
            args: ["artifact_id": .string(artifactId), "tail_lines": .int(tailLines)]
        )
    }

    func artifactData(artifactId: String) async throws -> ArtifactPayload {
        let client = try await ensureConnected()
        let content = try await client.readResource(uri: "devkit://artifact/\(artifactId)")
        if let blob = content.blob {
            guard let data = Data(base64Encoded: blob) else {
                throw BridgeError.protocolViolation(
                    "resources/read for \(artifactId): invalid base64 blob")
            }
            return ArtifactPayload(
                data: data, mimeType: content.mimeType ?? "application/octet-stream")
        }
        if let text = content.text {
            return ArtifactPayload(data: Data(text.utf8), mimeType: content.mimeType ?? "text/plain")
        }
        throw BridgeError.protocolViolation(
            "resources/read for \(artifactId): contents[0] has neither blob nor text")
    }

    // MARK: - connection management

    /// Auto-connect: the first call after construction, death, or disconnect
    /// spawns and handshakes a fresh server. Concurrent first calls share one
    /// in-flight connect task.
    private func ensureConnected() async throws -> MCPClient {
        if let client, await client.isAlive() { return client }
        if let inFlight = connectTask { return try await inFlight.value }
        let task = Task<MCPClient, Error> { [launchConfig, progressContinuation] in
            let transport = StdioTransport(config: launchConfig)
            let client = MCPClient(transport: transport) { notification in
                progressContinuation.yield(notification)
            }
            try await client.connect()
            return client
        }
        connectTask = task
        defer { connectTask = nil }
        let connected = try await task.value
        client = connected
        return connected
    }

    // MARK: - tool plumbing

    private func callTool<T: Decodable>(
        _ type: T.Type,
        _ name: String,
        args: [String: JSONValue] = [:],
        timeout: TimeInterval = MCPBridge.defaultTimeout,
        progressToken: String? = nil
    ) async throws -> T {
        let client = try await ensureConnected()
        let outcome = try await client.callTool(
            name: name, arguments: args, timeout: timeout, progressToken: progressToken)
        if outcome.isError { throw Self.mapToolError(outcome, tool: name) }
        return try Self.decodePayload(type, from: outcome, tool: name)
    }

    /// The `JobStarted` wire payload (`{"job_id", "status"}` in mcp_server.py);
    /// `JobStart` additionally carries our client-generated progress token.
    private struct JobStartedWire: Decodable {
        var jobId: String
        var status: String
    }

    private func startJob(_ name: String, args: [String: JSONValue]) async throws -> JobStart {
        // A fresh token per job; the server bridges journal progress to
        // connection-level notifications/progress carrying it. Notifications
        // for this token may arrive before the call returns — the stream
        // forwards them regardless; the UI correlates.
        let token = "gui-" + UUID().uuidString
        let wire: JobStartedWire = try await callTool(
            JobStartedWire.self, name, args: args, progressToken: token)
        return JobStart(jobId: wire.jobId, progressToken: token)
    }

    /// Prefer `structuredContent`; fall back to `content[0].text` as JSON.
    static func decodePayload<T: Decodable>(
        _ type: T.Type, from outcome: MCPClient.CallToolOutcome, tool: String
    ) throws -> T {
        if let structured = outcome.structured {
            // mcp SDK 2.3.0 wraps non-object structured output: arrays come
            // back as {"result": [...]} — unwrap for collection decodes.
            var payload = structured
            if T.self is any Collection.Type,
               case .object(let object) = structured,
               object.count == 1, let inner = object["result"] {
                payload = inner
            }
            do {
                let data = try JSONEncoder().encode(payload)
                return try DevkitJSON.makeDecoder().decode(T.self, from: data)
            } catch {
                throw BridgeError.decoding(
                    "\(tool): structured content did not decode as \(T.self): \(error)")
            }
        }
        guard let text = outcome.texts.first else {
            throw BridgeError.protocolViolation(
                "\(tool): success result had neither structuredContent nor text content")
        }
        do {
            return try DevkitJSON.makeDecoder().decode(T.self, from: Data(text.utf8))
        } catch {
            throw BridgeError.decoding("\(tool): text content did not decode as \(T.self): \(error)")
        }
    }

    /// isError text → DevkitErrorPayload. The real server prefixes the payload
    /// with "Error executing tool <name>: " (mcp SDK 2.3.0 ToolError path);
    /// the bare-JSON form is accepted too. Anything else is a protocol
    /// violation carrying the raw text.
    static func mapToolError(_ outcome: MCPClient.CallToolOutcome, tool: String) -> BridgeError {
        let text = outcome.texts.first ?? ""
        if let payload = decodeErrorPayload(text) { return .devkit(payload) }
        return .protocolViolation("\(tool): isError text was not a DevkitError payload: \(text)")
    }

    static func decodeErrorPayload(_ text: String) -> DevkitErrorPayload? {
        let decoder = DevkitJSON.makeDecoder()
        if let payload = try? decoder.decode(DevkitErrorPayload.self, from: Data(text.utf8)) {
            return payload
        }
        if let brace = text.firstIndex(of: "{") {
            let tail = String(text[brace...])
            if let payload = try? decoder.decode(DevkitErrorPayload.self, from: Data(tail.utf8)) {
                return payload
            }
        }
        return nil
    }

    // MARK: - argument builders (hand-built: exact wire names, no encoder
    // key-strategy surprises for user-controlled dictionary keys)

    private func deviceArgs(_ device: String) -> [String: JSONValue] {
        ["device": .string(device)]
    }

    private func titleArgs(_ device: String, _ gameid: String) -> [String: JSONValue] {
        ["device": .string(device), "gameid": .string(gameid)]
    }

    private func toggleArgs(_ device: String, _ enabled: Bool) -> [String: JSONValue] {
        ["device": .string(device), "enabled": .bool(enabled)]
    }

    private static func filtersArgs(_ filters: FilterSpec) -> JSONValue {
        .object([
            "mode": .string(filters.mode.rawValue),
            "patterns": .array(filters.patterns.map { .string($0) }),
        ])
    }

    private static func configValue(_ config: TitleConfig) -> JSONValue {
        var object: [String: JSONValue] = [
            "args": .array(config.args.map { .string($0) }),
            "env": .object(config.env.mapValues { .string($0) }),
            "settings": .object(config.settings.mapValues { .string($0) }),
        ]
        if let entrypoint = config.entrypoint { object["entrypoint"] = .string(entrypoint) }
        if let runtime = config.runtime { object["runtime"] = .string(runtime.rawValue) }
        return .object(object)
    }

    // MARK: - test hooks (internal; not part of the DevkitBridge protocol)

    /// The live client for tests that need raw JSON-RPC access.
    func testingClient() async throws -> MCPClient {
        try await ensureConnected()
    }
}
