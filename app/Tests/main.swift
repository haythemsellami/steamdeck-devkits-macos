import Foundation

// CLT-only test runner: executes every registered group sequentially, prints
// PASS/FAIL per test, exits 1 on any failure.

actor TestResults {
    private(set) var passed = 0
    private(set) var failures: [String] = []

    func pass() { passed += 1 }
    func fail(_ name: String) { failures.append(name) }
}

func runGroup(_ group: String, tests: [(String, () async throws -> Void)], into results: TestResults) async {
    for (name, body) in tests {
        do {
            try await body()
            await results.pass()
            print("PASS \(group)/\(name)")
        } catch {
            await results.fail("\(group)/\(name)")
            print("FAIL \(group)/\(name) — \(error)")
        }
    }
}

let results = TestResults()
await runGroup("bridge", tests: AllTests.bridgeTests, into: results)
await runGroup("viewmodel", tests: AllTests.viewModelTests, into: results)

let passed = await results.passed
let failures = await results.failures
let total = passed + failures.count
if failures.isEmpty {
    print("== \(total)/\(total) tests passed")
    exit(0)
} else {
    print("== \(failures.count)/\(total) FAILED: \(failures.joined(separator: ", "))")
    exit(1)
}
