import Foundation

// VMTestSupport.swift — view-model test helpers that TestKit does not
// provide. Assertions throw TestKit's `TestIssue` so failures render like
// every other harness test.

/// Shorthand failure for guard/else paths in view-model tests.
struct VMTestFailure: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

func expectNil<T>(
    _ value: @autoclosure () throws -> T?,
    _ message: @autoclosure () -> String = "",
    file: StaticString = #filePath, line: UInt = #line
) throws {
    if try value() != nil {
        let text = message()
        throw TestIssue(text.isEmpty ? "expected nil" : text, file: file, line: line)
    }
}

func expectNotNil<T>(
    _ value: @autoclosure () throws -> T?,
    _ message: @autoclosure () -> String = "",
    file: StaticString = #filePath, line: UInt = #line
) throws {
    if try value() == nil {
        let text = message()
        throw TestIssue(text.isEmpty ? "expected non-nil" : text, file: file, line: line)
    }
}

/// Polls a condition until it holds or the deadline passes.
func waitFor(
    _ description: String, timeout: Double = 5,
    file: StaticString = #filePath, line: UInt = #line,
    _ condition: @Sendable () async -> Bool
) async throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if await condition() { return }
        try await Task.sleep(for: .milliseconds(10))
    }
    throw TestIssue("timed out waiting for: \(description)", file: file, line: line)
}

/// Races an async operation against a deadline.
func withTimeout<T: Sendable>(
    _ seconds: Double, _ what: String,
    file: StaticString = #filePath, line: UInt = #line,
    _ operation: @escaping @Sendable () async throws -> T
) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask { try await operation() }
        group.addTask {
            try await Task.sleep(for: .seconds(seconds))
            throw TestIssue("timed out (\(seconds)s): \(what)", file: file, line: line)
        }
        guard let value = try await group.next() else {
            throw TestIssue("task group empty: \(what)", file: file, line: line)
        }
        group.cancelAll()
        return value
    }
}
