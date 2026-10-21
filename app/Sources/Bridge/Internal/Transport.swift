import Foundation

/// Lock-protected accumulator that splits arbitrary byte chunks into complete
/// lines (\n-delimited, UTF-8). Callable from the readabilityHandler queue;
/// tolerates arbitrarily long lines and partial reads. A trailing partial
/// line is emitted on `flush()`.
final class LineSplitter: @unchecked Sendable {
    private var buffer = Data()
    private let lock = NSLock()

    func append(_ data: Data) -> [String] {
        lock.lock()
        defer { lock.unlock() }
        buffer.append(data)
        var lines: [String] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            let lineData = buffer.subdata(in: buffer.startIndex..<newline)
            buffer.removeSubrange(buffer.startIndex...newline)
            if let line = String(data: lineData, encoding: .utf8) {
                lines.append(line)
            }
        }
        return lines
    }

    /// Any unterminated remainder, once, at EOF.
    func flush() -> String? {
        lock.lock()
        defer { lock.unlock() }
        guard !buffer.isEmpty else { return nil }
        let remainder = buffer
        buffer.removeAll()
        return String(data: remainder, encoding: .utf8)
    }
}

/// Bounded last-N-lines ring for server stderr (diagnostics only).
final class StderrRing: @unchecked Sendable {
    private var lines: [String] = []
    private let limit: Int
    private let lock = NSLock()

    init(limit: Int) { self.limit = limit }

    func append(_ line: String) {
        lock.lock()
        defer { lock.unlock() }
        lines.append(line)
        if lines.count > limit {
            lines.removeFirst(lines.count - limit)
        }
    }

    func snapshot() -> [String] {
        lock.lock()
        defer { lock.unlock() }
        return lines
    }
}

/// Process + byte plumbing for one MCP stdio server (newline-delimited
/// JSON-RPC 2.0, UTF-8, one message per line).
///
/// - stdout/stderr are read with `FileHandle.readabilityHandler` (serialized
///   per handle), split into lines by a lock-protected `LineSplitter` — this
///   tolerates arbitrarily long lines (multi-MB base64 artifact blobs) and
///   partial reads, and delivers lines in wire order. (`FileHandle.bytes.lines`
///   was tried first and silently never delivered in this configuration.)
/// - stderr goes to a bounded ring (last 100 lines) for diagnostics; it
///   carries server logs, never protocol bytes.
/// - On unexpected process termination or stdout EOF, `termination` yields
///   once with a human-readable reason and both streams finish. The JSONRPC
///   layer turns that into `BridgeError.transport` for all pending requests.
actor StdioTransport {
    struct Diagnostics: Sendable, Equatable {
        var running: Bool
        var stderrTail: [String]
    }

    private enum State: Sendable {
        case idle
        case running
        case closed
    }

    private let config: BridgeLaunchConfig
    private var state: State = .idle
    private var process: Process?
    private var stdinHandle: FileHandle?
    private var readHandle: FileHandle?
    private var errorHandle: FileHandle?

    private nonisolated let stderrRing = StderrRing(limit: 100)

    /// Complete stdout lines in wire order; finished when the transport ends.
    nonisolated let lines: AsyncStream<String>
    /// Exactly one element (a human-readable reason) on process death, then
    /// finished. On intentional `close()` the stream finishes silently.
    nonisolated let termination: AsyncStream<String>
    private let lineContinuation: AsyncStream<String>.Continuation
    private let terminationContinuation: AsyncStream<String>.Continuation

    init(config: BridgeLaunchConfig) {
        self.config = config
        var lineContinuation: AsyncStream<String>.Continuation!
        self.lines = AsyncStream { lineContinuation = $0 }
        var terminationContinuation: AsyncStream<String>.Continuation!
        self.termination = AsyncStream { terminationContinuation = $0 }
        self.lineContinuation = lineContinuation
        self.terminationContinuation = terminationContinuation
    }

    var isRunning: Bool { state == .running }

    /// Sanitized base environment (PATH, HOME, UV_*) merged with the launch
    /// config's entries (which win on conflict).
    static func resolvedEnvironment(overrides: [String: String]) -> [String: String] {
        var environment: [String: String] = [:]
        let current = ProcessInfo.processInfo.environment
        if let path = current["PATH"] { environment["PATH"] = path }
        if let home = current["HOME"] { environment["HOME"] = home }
        for (key, value) in current where key.hasPrefix("UV_") {
            environment[key] = value
        }
        for (key, value) in overrides {
            environment[key] = value
        }
        return environment
    }

    func start() throws {
        guard state == .idle else { return }
        // Writing to a pipe whose read end closed must not kill the process.
        signal(SIGPIPE, SIG_IGN)

        let process = Process()
        if config.executable.contains("/") {
            process.executableURL = URL(fileURLWithPath: config.executable)
            process.arguments = config.arguments
        } else {
            // PATH lookup via env(1): presets use bare names ("uv", "python3").
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = [config.executable] + config.arguments
        }
        process.environment = Self.resolvedEnvironment(overrides: config.environment)
        if let workingDirectory = config.workingDirectory {
            process.currentDirectoryURL = URL(fileURLWithPath: workingDirectory)
        }

        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        process.terminationHandler = { [weak self] process in
            Task { await self?.died(reason: "exit code \(process.terminationStatus)") }
        }

        do {
            try process.run()
        } catch {
            throw BridgeError.transport(
                "spawn failed for \(config.executable): \(error.localizedDescription)")
        }

        state = .running
        self.process = process
        stdinHandle = stdinPipe.fileHandleForWriting

        // stdout: complete lines in wire order. readabilityHandler is
        // serialized per handle, and the splitter only emits complete lines in
        // arrival order, so yields are correctly ordered.
        let lineContinuation = self.lineContinuation
        let stdoutSplitter = LineSplitter()
        let readHandle = stdoutPipe.fileHandleForReading
        self.readHandle = readHandle
        readHandle.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                if let remainder = stdoutSplitter.flush() {
                    lineContinuation.yield(remainder)
                }
                lineContinuation.finish()
                Task { await self?.stdoutEnded() }
                return
            }
            for line in stdoutSplitter.append(data) {
                lineContinuation.yield(line)
            }
        }

        // stderr: diagnostics ring only, appended synchronously (lock-safe).
        let stderrRing = self.stderrRing
        let stderrSplitter = LineSplitter()
        let errorHandle = stderrPipe.fileHandleForReading
        self.errorHandle = errorHandle
        errorHandle.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                if let remainder = stderrSplitter.flush() {
                    stderrRing.append(remainder)
                }
                return
            }
            for line in stderrSplitter.append(data) {
                stderrRing.append(line)
            }
        }
    }

    /// Write one frame (a newline is appended). Throws `BridgeError.transport`.
    func send(_ text: String) throws {
        guard state == .running, let handle = stdinHandle else {
            throw BridgeError.transport("not connected")
        }
        var data = Data(text.utf8)
        data.append(0x0A)
        do {
            try handle.write(contentsOf: data)
        } catch {
            throw BridgeError.transport("write failed: \(error.localizedDescription)")
        }
    }

    /// Intentional shutdown: terminate the process, end the streams, stop the
    /// readers. Pending requests are failed by the JSONRPC layer's own close
    /// path, which runs first.
    func close() async {
        guard state != .closed else { return }
        state = .closed
        let process = self.process
        self.process = nil
        readHandle?.readabilityHandler = nil
        errorHandle?.readabilityHandler = nil
        lineContinuation.finish()
        terminationContinuation.finish()
        try? stdinHandle?.close()
        stdinHandle = nil
        process?.terminate()
    }

    func diagnostics() -> Diagnostics {
        Diagnostics(running: state == .running, stderrTail: stderrRing.snapshot())
    }

    private func stdoutEnded() {
        // stdout EOF means the server is gone (or closing); treat as death.
        died(reason: "stdout closed")
    }

    private func died(reason: String) {
        guard state == .running else { return }
        state = .closed
        process = nil
        readHandle?.readabilityHandler = nil
        errorHandle?.readabilityHandler = nil
        terminationContinuation.yield(reason)
        lineContinuation.finish()
        terminationContinuation.finish()
        try? stdinHandle?.close()
        stdinHandle = nil
    }
}
