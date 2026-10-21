import Foundation

/// Minimal assertion kit for the CLT-only test harness (no XCTest).
/// Assertions throw `TestIssue` carrying file/line; the runner in main.swift
/// catches per test, prints FAIL with the reason, and keeps going.
struct TestIssue: Error, CustomStringConvertible {
    var message: String
    var file: String
    var line: UInt

    init(_ message: String, file: StaticString, line: UInt) {
        self.message = message
        self.file = "\(file)"
        self.line = line
    }

    var description: String { "\(file):\(line): \(message)" }
}

func expect(
    _ condition: @autoclosure () throws -> Bool,
    _ message: @autoclosure () -> String = "",
    file: StaticString = #filePath, line: UInt = #line
) throws {
    if try !condition() {
        let text = message()
        throw TestIssue(text.isEmpty ? "expectation failed" : text, file: file, line: line)
    }
}

func expectEqual<T: Equatable>(
    _ actual: @autoclosure () throws -> T,
    _ expected: @autoclosure () throws -> T,
    _ message: @autoclosure () -> String = "",
    file: StaticString = #filePath, line: UInt = #line
) throws {
    let actualValue = try actual()
    let expectedValue = try expected()
    if actualValue != expectedValue {
        let text = message()
        throw TestIssue(
            "expected \(expectedValue), got \(actualValue)" + (text.isEmpty ? "" : " — \(text)"),
            file: file, line: line
        )
    }
}

/// Assert the body throws. Returns the caught error for case inspection.
@discardableResult
func expectThrows<T>(
    _ message: @autoclosure () -> String = "expected an error, but the call succeeded",
    file: StaticString = #filePath, line: UInt = #line,
    _ body: () async throws -> T
) async throws -> Error {
    do {
        _ = try await body()
    } catch {
        return error
    }
    throw TestIssue(message(), file: file, line: line)
}

/// Assert the body throws `BridgeError.devkit` with the given wire code.
func expectDevkitError(
    _ code: String,
    file: StaticString = #filePath, line: UInt = #line,
    _ body: () async throws -> some Any
) async throws {
    let error = try await expectThrows("expected BridgeError.devkit(\(code))", file: file, line: line, body)
    guard let bridgeError = error as? BridgeError else {
        throw TestIssue("expected BridgeError.devkit(\(code)), got \(error)", file: file, line: line)
    }
    guard case .devkit(let payload) = bridgeError else {
        throw TestIssue("expected BridgeError.devkit(\(code)), got \(bridgeError)", file: file, line: line)
    }
    try expectEqual(payload.code, code, file: file, line: line)
}

/// A counting box for stream-collection helpers.
actor EventBox<T: Sendable> {
    private var values: [T] = []

    func append(_ value: T) { values.append(value) }
    var count: Int { values.count }
    func snapshot() -> [T] { values }
}

/// Consume `stream` in the background, appending matching elements to `box`.
@discardableResult
func collectAsync<T: Sendable>(
    _ stream: AsyncStream<T>,
    matching predicate: @escaping @Sendable (T) -> Bool,
    into box: EventBox<T>
) -> Task<Void, Never> {
    Task {
        for await element in stream where predicate(element) {
            await box.append(element)
        }
    }
}

/// Poll `box` until it holds `count` elements or the deadline passes.
func waitForCount<T: Sendable>(
    _ box: EventBox<T>,
    _ count: Int,
    timeout: TimeInterval,
    file: StaticString = #filePath, line: UInt = #line
) async throws -> [T] {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        let snapshot = await box.snapshot()
        if snapshot.count >= count { return snapshot }
        try await Task.sleep(for: .milliseconds(50))
    }
    let got = await box.count
    throw TestIssue("timed out waiting for \(count) events (got \(got))", file: file, line: line)
}
