import SwiftUI

@main
struct DevkitSpikeApp: App {
    var body: some Scene {
        WindowGroup("Devkit bridge spike") {
            ContentView()
        }
    }
}

struct ContentView: View {
    @State private var output = "Press a button to run a bundled tool."

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Devkit bridge spike")
                .font(.title)
            HStack(spacing: 12) {
                Button("Run bundled Python") { output = runPython() }
                Button("Run bundled rsync") { output = runRsync() }
            }
            ScrollView {
                Text(output)
                    .font(.system(.body, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .padding(4)
            }
            .border(.secondary)
        }
        .padding()
        .frame(minWidth: 560, minHeight: 340)
    }
}

private func runPython() -> String {
    run(
        bundled("python/bin/python3"),
        ["-c", "import sys,platform;print(sys.version);print(platform.machine())"]
    )
}

private func runRsync() -> String {
    let full = run(bundled("bin/rsync"), ["--version"])
    guard let first = full.components(separatedBy: .newlines).first(where: { !$0.isEmpty }) else {
        return full
    }
    return first + "\n(full --version output truncated to first line)"
}

private func bundled(_ relative: String) -> String {
    guard let resources = Bundle.main.resourceURL else {
        return relative
    }
    return resources.appendingPathComponent(relative).path
}

private func run(_ executable: String, _ arguments: [String]) -> String {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.standardOutput = pipe
    process.standardError = pipe
    do {
        try process.run()
    } catch {
        return "launch failed for \(executable): \(error.localizedDescription)"
    }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    let text = String(decoding: data, as: UTF8.self)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    let body = text.isEmpty ? "(no output)" : text
    return "$ \(executable)\nexit: \(process.terminationStatus)\n\(body)"
}
