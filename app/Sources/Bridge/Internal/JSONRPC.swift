import Foundation

// JSON-RPC 2.0 framing over StdioTransport: request ids (monotonically
// increasing Int64), a pending-request registry (id → continuation), response
// routing, and notification dispatch.
//
// Malformed lines are counted and skipped (never fatal); responses with
// unknown ids are ignored (counted). Both counts surface through
// `diagnostics()`.

/// A JSON-RPC error object from a response frame.
struct RPCErrorInfo: Error, Sendable, Equatable {
    var code: Int
    var message: String
}

/// One server→client notification (e.g. `notifications/progress`).
struct RPCNotification: Sendable, Equatable {
    var method: String
    var params: JSONValue?
}

/// Framing diagnostics counters (the malformed-line diagnostics hook).
struct JSONRPCDiagnostics: Sendable, Equatable {
    var malformedLines: Int
    var unknownResponses: Int
    var requestsSent: Int
}

/// One decoded inbound frame.
private enum RPCMessage: Sendable {
    case response(id: Int64, result: Swift.Result<JSONValue, RPCErrorInfo>)
    case notification(method: String, params: JSONValue?)
    /// A request from the server; answered with -32601 (we advertise no
    /// client capabilities, so the server should never send one).
    case serverRequest(id: JSONValue, method: String)
}

extension JSONValue {
    var stringValue: String? { if case .string(let value) = self { value } else { nil } }
    var boolValue: Bool? { if case .bool(let value) = self { value } else { nil } }
    var intValue: Int? { if case .int(let value) = self { value } else { nil } }
    var doubleValue: Double? {
        switch self {
        case .double(let value): return value
        case .int(let value): return Double(value)
        default: return nil
        }
    }
    var objectValue: [String: JSONValue]? { if case .object(let value) = self { value } else { nil } }
    var arrayValue: [JSONValue]? { if case .array(let value) = self { value } else { nil } }
}

actor JSONRPCSession {
    private nonisolated let transport: StdioTransport

    private var nextId: Int64 = 0
    private var alive = false
    private var pending: [Int64: CheckedContinuation<JSONValue, Error>] = [:]
    private var pumpTask: Task<Void, Never>?
    private var terminationTask: Task<Void, Never>?

    private var malformedLines = 0
    private var unknownResponses = 0
    private var requestsSent = 0

    /// Server→client notifications, in wire order. Finished on close/death.
    nonisolated let notifications: AsyncStream<RPCNotification>
    private let notificationsContinuation: AsyncStream<RPCNotification>.Continuation

    init(transport: StdioTransport) {
        self.transport = transport
        var continuation: AsyncStream<RPCNotification>.Continuation!
        self.notifications = AsyncStream { continuation = $0 }
        self.notificationsContinuation = continuation
    }

    var isAlive: Bool { alive }

    func start() {
        guard !alive else { return }
        alive = true
        pumpTask = Task.detached { [weak self] in
            guard let self else { return }
            for await line in self.transport.lines {
                await self.handleLine(line)
            }
            await self.transportDied(reason: "stdout closed")
        }
        terminationTask = Task.detached { [weak self] in
            guard let self else { return }
            for await reason in self.transport.termination {
                await self.transportDied(reason: reason)
            }
        }
    }

    /// One request/response round trip. `timeout` (seconds) fails the call
    /// with `BridgeError.transport` and notifies the server
    /// (`notifications/cancelled`); task cancellation does the same with a
    /// `CancellationError` for the caller.
    func call(method: String, params: JSONValue?, timeout: TimeInterval) async throws -> JSONValue {
        guard alive else { throw BridgeError.transport("not connected") }
        let id = nextId
        nextId += 1
        requestsSent += 1
        let frame = Self.requestFrame(id: id, method: method, params: params)
        return try await withTaskCancellationHandler {
            try await withThrowingTaskGroup(of: JSONValue.self) { group in
                group.addTask { [self] in
                    try await self.awaitResponse(id: id, frame: frame)
                }
                group.addTask { [self] in
                    try await Task.sleep(for: .milliseconds(UInt64(timeout * 1000)))
                    let error = BridgeError.transport(
                        "timed out after \(Int(timeout))s waiting for \(method)")
                    await self.requestFailed(id: id, error: error)
                    await self.sendCancellationNotice(requestId: id, reason: "timeout")
                    throw error
                }
                defer { group.cancelAll() }
                guard let first = try await group.next() else {
                    throw BridgeError.transport("internal error: empty response group")
                }
                return first
            }
        } onCancel: {
            Task { await self.requestCancelled(id: id) }
        }
    }

    /// Fire-and-forget notification (send errors propagate to the caller).
    func notify(method: String, params: JSONValue?) async throws {
        guard alive else { throw BridgeError.transport("not connected") }
        var frame: [String: JSONValue] = [
            "jsonrpc": .string("2.0"),
            "method": .string(method),
        ]
        if let params { frame["params"] = params }
        try await transport.send(Self.serialize(.object(frame)))
    }

    /// Intentional shutdown: fail pendings, finish the notification stream,
    /// close the transport.
    func close() async {
        guard alive else { return }
        alive = false
        failAll(BridgeError.transport("disconnected"))
        notificationsContinuation.finish()
        pumpTask?.cancel()
        terminationTask?.cancel()
        await transport.close()
    }

    func diagnostics() -> JSONRPCDiagnostics {
        JSONRPCDiagnostics(
            malformedLines: malformedLines,
            unknownResponses: unknownResponses,
            requestsSent: requestsSent
        )
    }

    // MARK: - pending registry

    private func awaitResponse(id: Int64, frame: JSONValue) async throws -> JSONValue {
        try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            Task { await self.transmit(id: id, frame: frame) }
        }
    }

    private func transmit(id: Int64, frame: JSONValue) async {
        // The call may have been cancelled or timed out before the write.
        guard pending[id] != nil else { return }
        do {
            try await transport.send(Self.serialize(frame))
        } catch {
            requestFailed(id: id, error: error)
        }
    }

    private func requestFailed(id: Int64, error: Error) {
        guard let continuation = pending.removeValue(forKey: id) else { return }
        continuation.resume(throwing: error)
    }

    private func requestCancelled(id: Int64) async {
        guard let continuation = pending.removeValue(forKey: id) else { return }
        continuation.resume(throwing: CancellationError())
        await sendCancellationNotice(requestId: id, reason: "cancelled by client")
    }

    private func failAll(_ error: Error) {
        let continuations = pending
        pending.removeAll()
        for (_, continuation) in continuations {
            continuation.resume(throwing: error)
        }
    }

    private func sendCancellationNotice(requestId: Int64, reason: String) async {
        guard alive else { return }
        let frame: JSONValue = .object([
            "jsonrpc": .string("2.0"),
            "method": .string("notifications/cancelled"),
            "params": .object([
                "requestId": .int(Int(requestId)),
                "reason": .string(reason),
            ]),
        ])
        try? await transport.send(Self.serialize(frame))
    }

    // MARK: - inbound routing

    private func transportDied(reason: String) {
        guard alive else { return }
        alive = false
        failAll(BridgeError.transport("server process ended: \(reason)"))
        notificationsContinuation.finish()
        pumpTask?.cancel()
        terminationTask?.cancel()
    }

    private func handleLine(_ line: String) {
        guard let message = Self.parse(line) else {
            malformedLines += 1
            return
        }
        switch message {
        case .response(let id, let outcome):
            guard let continuation = pending.removeValue(forKey: id) else {
                unknownResponses += 1
                return
            }
            switch outcome {
            case .success(let value):
                continuation.resume(returning: value)
            case .failure(let error):
                continuation.resume(throwing: BridgeError.rpc(code: error.code, message: error.message))
            }
        case .notification(let method, let params):
            notificationsContinuation.yield(RPCNotification(method: method, params: params))
        case .serverRequest(let id, let method):
            Task { await self.rejectServerRequest(id: id, method: method) }
        }
    }

    private func rejectServerRequest(id: JSONValue, method: String) async {
        let frame: JSONValue = .object([
            "jsonrpc": .string("2.0"),
            "id": id,
            "error": .object([
                "code": .int(-32601),
                "message": .string("Method not found: \(method)"),
            ]),
        ])
        try? await transport.send(Self.serialize(frame))
    }

    // MARK: - framing

    private static func requestFrame(id: Int64, method: String, params: JSONValue?) -> JSONValue {
        var frame: [String: JSONValue] = [
            "jsonrpc": .string("2.0"),
            "id": .int(Int(id)),
            "method": .string(method),
        ]
        if let params { frame["params"] = params }
        return .object(frame)
    }

    static func serialize(_ value: JSONValue) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let text = String(data: data, encoding: .utf8) else {
            fatalError("JSONValue is always encodable")
        }
        return text
    }

    /// Parse one inbound line. `nil` means malformed (caller counts + skips).
    private static func parse(_ line: String) -> RPCMessage? {
        guard let data = line.data(using: .utf8),
              let value = try? JSONDecoder().decode(JSONValue.self, from: data),
              case .object(let object) = value,
              object["jsonrpc"]?.stringValue == "2.0" else {
            return nil
        }
        let method = object["method"]?.stringValue
        let id = object["id"]
        if let method, id == nil {
            return .notification(method: method, params: object["params"])
        }
        if let method, let id {
            return .serverRequest(id: id, method: method)
        }
        if let id {
            // Ids we cannot normalize route to the never-registered id -1,
            // which lands in the unknown-response path (ignored + counted).
            let numericId = Self.numericId(id) ?? -1
            if case .object(let error) = object["error"] {
                guard let code = error["code"]?.intValue,
                      let message = error["message"]?.stringValue else { return nil }
                return .response(id: numericId, result: .failure(RPCErrorInfo(code: code, message: message)))
            }
            if let result = object["result"] {
                return .response(id: numericId, result: .success(result))
            }
        }
        return nil
    }

    /// Our request ids are Int64; accept numeric strings leniently.
    private static func numericId(_ value: JSONValue) -> Int64? {
        switch value {
        case .int(let id): return Int64(id)
        case .string(let text): return Int64(text)
        default: return nil
        }
    }
}
