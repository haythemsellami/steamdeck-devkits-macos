import Foundation

/// The MCP session on top of the JSON-RPC framing: initialize handshake,
/// tools/list, tools/call, resources/read, connection-level progress
/// notifications, and disconnect.
///
/// Handshake facts observed against the real server (`uv run devkit-mcp`,
/// mcp SDK 2.3.0, 2026-10-21; fixture `app/Tests/Fixtures/initialize_result.json`):
/// we offer protocolVersion "2025-06-18" and the server answers exactly
/// "2025-06-18" with serverInfo {"steamos-devkit", "0.0.0-dev"} and
/// capabilities {prompts, resources, tools} (each with listChanged: false).
///
/// Success result shape (observed): `structuredContent` is always present;
/// object payloads sit at its top level, array payloads are wrapped as
/// `{"result": [...]}`. A pretty-printed text copy is present in
/// `content[0].text` except for empty arrays (no text content at all).
///
/// Error result shape (observed): no `structuredContent`; `isError: true` and
/// `content[0].text` is `"Error executing tool <name>: <payload JSON>"` — the
/// SDK prefixes the DevkitError payload. MCPBridge strips that prefix.
actor MCPClient {
    /// What we offer in `initialize`.
    static let offeredProtocolVersion = "2025-06-18"

    struct ServerIdentity: Sendable, Equatable {
        var name: String
        var version: String
    }

    /// The decoded `tools/call` envelope (both payload channels preserved).
    struct CallToolOutcome: Sendable, Equatable {
        var structured: JSONValue?
        var texts: [String]
        var isError: Bool
    }

    /// `contents[0]` of a `resources/read` result.
    struct ResourceContent: Sendable, Equatable {
        var uri: String?
        var mimeType: String?
        var blob: String?
        var text: String?
    }

    private nonisolated let transport: StdioTransport
    private nonisolated let rpc: JSONRPCSession
    private let onProgress: @Sendable (ProgressNotification) -> Void
    private var pumpTask: Task<Void, Never>?
    private var handshaken = false

    /// The protocol version the server selected in its initialize result.
    private(set) var serverProtocolVersion: String?
    private(set) var serverIdentity: ServerIdentity?

    init(
        transport: StdioTransport,
        onProgress: @escaping @Sendable (ProgressNotification) -> Void
    ) {
        self.transport = transport
        self.rpc = JSONRPCSession(transport: transport)
        self.onProgress = onProgress
    }

    /// Alive means handshaken and the transport is still up.
    func isAlive() async -> Bool {
        guard handshaken else { return false }
        return await rpc.isAlive
    }

    /// Spawn the server and handshake. Idempotent while alive; a failed
    /// handshake tears the transport down so a retry starts fresh.
    func connect() async throws {
        guard !handshaken else { return }
        do {
            try await transport.start()
            await rpc.start()
            let params: JSONValue = .object([
            "protocolVersion": .string(Self.offeredProtocolVersion),
            "capabilities": .object([:]),
            "clientInfo": .object([
                "name": .string("steamos-devkit-gui"),
                "version": .string(Self.clientVersion),
            ]),
        ])
        let result = try await rpc.call(method: "initialize", params: params, timeout: 60)
        guard let object = result.objectValue,
              let version = object["protocolVersion"]?.stringValue else {
            throw BridgeError.protocolViolation("initialize result missing protocolVersion")
        }
        serverProtocolVersion = version
        if let info = object["serverInfo"]?.objectValue,
           let name = info["name"]?.stringValue {
            serverIdentity = ServerIdentity(name: name, version: info["version"]?.stringValue ?? "")
        }
            try await rpc.notify(method: "notifications/initialized", params: nil)
            handshaken = true
            startProgressPump()
        } catch {
            await rpc.close()
            throw error
        }
    }

    func disconnect() async {
        handshaken = false
        pumpTask?.cancel()
        pumpTask = nil
        await rpc.close()
    }

    func callTool(
        name: String,
        arguments: [String: JSONValue] = [:],
        timeout: TimeInterval = 60,
        progressToken: String? = nil
    ) async throws -> CallToolOutcome {
        guard handshaken else { throw BridgeError.transport("not connected") }
        var params: [String: JSONValue] = [
            "name": .string(name),
            "arguments": .object(arguments),
        ]
        if let progressToken {
            params["_meta"] = .object(["progressToken": .string(progressToken)])
        }
        let result = try await rpc.call(method: "tools/call", params: .object(params), timeout: timeout)
        return try Self.parseToolOutcome(result, tool: name)
    }

    func listToolNames() async throws -> [String] {
        guard handshaken else { throw BridgeError.transport("not connected") }
        let result = try await rpc.call(method: "tools/list", params: nil, timeout: 60)
        guard let tools = result.objectValue?["tools"]?.arrayValue else {
            throw BridgeError.protocolViolation("tools/list result missing tools array")
        }
        return tools.compactMap { $0.objectValue?["name"]?.stringValue }
    }

    func readResource(uri: String, timeout: TimeInterval = 60) async throws -> ResourceContent {
        guard handshaken else { throw BridgeError.transport("not connected") }
        let result = try await rpc.call(
            method: "resources/read", params: .object(["uri": .string(uri)]), timeout: timeout)
        guard let contents = result.objectValue?["contents"]?.arrayValue,
              let first = contents.first?.objectValue else {
            throw BridgeError.protocolViolation("resources/read result missing contents[0]")
        }
        return ResourceContent(
            uri: first["uri"]?.stringValue,
            mimeType: first["mimeType"]?.stringValue,
            blob: first["blob"]?.stringValue,
            text: first["text"]?.stringValue
        )
    }

    // MARK: - test/diagnostic surface (not part of the bridge protocol)

    /// Raw JSON-RPC call for tests (unknown-method errors, framing probes).
    func rawCall(method: String, params: JSONValue?, timeout: TimeInterval = 60) async throws -> JSONValue {
        try await rpc.call(method: method, params: params, timeout: timeout)
    }

    func rpcDiagnostics() async -> JSONRPCDiagnostics {
        await rpc.diagnostics()
    }

    func transportDiagnostics() async -> StdioTransport.Diagnostics {
        await transport.diagnostics()
    }

    // MARK: - internals

    private static var clientVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev"
    }

    private func startProgressPump() {
        let rpc = self.rpc
        let onProgress = self.onProgress
        pumpTask = Task.detached {
            for await notification in rpc.notifications {
                // Connection-level progress only; everything else is ignored.
                guard notification.method == "notifications/progress",
                      let params = notification.params?.objectValue,
                      let tokenValue = params["progressToken"],
                      let progress = params["progress"]?.doubleValue else { continue }
                let token: String
                switch tokenValue {
                case .string(let value): token = value
                case .int(let value): token = String(value)
                default: continue
                }
                onProgress(ProgressNotification(
                    progressToken: token,
                    progress: progress,
                    total: params["total"]?.doubleValue,
                    message: params["message"]?.stringValue
                ))
            }
        }
    }

    private static func parseToolOutcome(_ result: JSONValue, tool: String) throws -> CallToolOutcome {
        guard let object = result.objectValue else {
            throw BridgeError.protocolViolation("\(tool): tools/call result was not an object")
        }
        var texts: [String] = []
        if let content = object["content"]?.arrayValue {
            for entry in content {
                if let entryObject = entry.objectValue,
                   entryObject["type"]?.stringValue == "text",
                   let text = entryObject["text"]?.stringValue {
                    texts.append(text)
                }
            }
        }
        return CallToolOutcome(
            structured: object["structuredContent"],
            texts: texts,
            isError: object["isError"]?.boolValue ?? false
        )
    }
}
