import Foundation

/// Carry a non-Sendable Foundation handle into the single detached reader
/// task that exclusively owns it. Only ever read from that one task.
struct UnsafeSendableBox<Value>: @unchecked Sendable {
    let value: Value
}

/// Process + byte plumbing for one MCP stdio server (newline-delimited
/// JSON-RPC 2.0, UTF-8, one message per line).
///
/// - stdout is consumed with `FileHandle.bytes.lines`, which tolerates
///   arbitrarily long lines and partial reads (multi-MB base64 artifact blobs
///   come through as single lines).
/// - stderr is drained into a bounded ring (last 100 lines) for diagnostics;
///   it carries server logs, never protocol bytes.
/// - On unexpected process termination every consumer observes it:
///   `termination` yields once with a human-readable reason and both streams
///   finish. The JSONRPC layer turns that into `BridgeError.transport` for
///   all pending requests.
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
    private var readerTask: Task<Void, Never>?
    private var stderrTask: Task<Void, Never>?

    private var stderrRing: [String] = []
    private let stderrLimit = 100

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
            self?.processTerminated(process)
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

        let stdout = UnsafeSendableBox(value: stdoutPipe.fileHandleForReading)
        let lineContinuation = self.lineContinuation
        readerTask = Task.detached { [weak self] in
            do {
                for try await line in stdout.value.bytes.lines {
                    lineContinuation.yield(line)
                }
            } catch {
                // Cancellation or read failure; the termination path reports.
            }
            await self?.stdoutEnded()
        }

        let stderr = UnsafeSendableBox(value: stderrPipe.fileHandleForReading)
        stderrTask = Task.detached { [weak self] in
            do {
                for try await line in stderr.value.bytes.lines {
                    await self?.appendStderr(line)
                }
            } catch {
                // Cancellation or read failure; stderr is diagnostics only.
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

    /// Intentional shutdown: terminate the process, end the streams, and stop
    /// the readers. Pending requests are failed by the JSONRPC layer's own
    /// close path, which runs first.
    func close() async {
        guard state != .closed else { return }
        state = .closed
        let process = self.process
        self.process = nil
        lineContinuation.finish()
        terminationContinuation.finish()
        readerTask?.cancel()
        stderrTask?.cancel()
        try? stdinHandle?.close()
        stdinHandle = nil
        process?.terminate()
    }

    func diagnostics() -> Diagnostics {
        Diagnostics(running: state == .running, stderrTail: stderrRing)
    }

    private func appendStderr(_ line: String) {
        stderrRing.append(line)
        if stderrRing.count > stderrLimit {
            stderrRing.removeFirst(stderrRing.count - stderrLimit)
        }
    }

    private func stdoutEnded() {
        // stdout EOF means the server is gone (or closing); treat as death.
        died(reason: "stdout closed")
    }

    private nonisolated func processTerminated(_ process: Process) {
        Task { await self.died(reason: "exit code \(process.terminationStatus)") }
    }

    private func died(reason: String) {
        guard state == .running else { return }
        state = .closed
        process = nil
        terminationContinuation.yield(reason)
        lineContinuation.finish()
        terminationContinuation.finish()
        readerTask?.cancel()
        stderrTask?.cancel()
        try? stdinHandle?.close()
        stdinHandle = nil
    }
}
