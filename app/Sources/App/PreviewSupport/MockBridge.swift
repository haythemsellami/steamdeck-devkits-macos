import CryptoKit
import Foundation

// MockBridge.swift — a full in-memory DevkitBridge for previews, view-model
// tests, and running the app before MCPBridge lands. No device contact.
//
// Scenarios script the world; `injectError(_:_:)` forces any method to throw
// (keys are the protocol method names: "pair", "deploy", "status", ...).
// `deployWithProgress` emits 10 progress events over ~2 s after `deploy`
// (pace configurable via `eventDelay`), then the job succeeds on the next
// `jobStatus`. Device fixtures use TEST-NET addresses (192.0.2.x) per
// AGENTS.md — never real IPs.

final class MockBridge: DevkitBridge, @unchecked Sendable {
    enum Scenario: Sendable {
        /// Nothing paired, no jobs/artifacts. Onboarding path.
        case empty
        /// A paired Deck + Frame with titles, jobs, artifacts.
        case populated
        /// Discover finds a Deck, but pairing throws PAIRING_DECLINED.
        case pairingDeclined
        /// Populated plus a scripted ~2 s deploy progress sequence.
        case deployWithProgress
    }

    let scenario: Scenario
    /// Pace between scripted progress events. Tests shrink this.
    var eventDelay: Duration {
        get { locked { _eventDelay } }
        set { locked { _eventDelay = newValue } }
    }

    private var _eventDelay: Duration = .milliseconds(200)
    private var devices: [Device]
    private var titles: [String: [Title]]
    private var titleDetails: [String: TitleDetail]  // "<device>/<gameid>"
    private var jobs: [String: JobInfo]
    private var artifacts: [ArtifactEntry]
    private var blobs: [String: Data]
    private var injectedErrors: [String: BridgeError] = [:]
    private var steamModes: [String: SteamStatus] = [:]
    private var jobCounter = 0
    private var tokenCounter = 0

    private let lock = NSLock()
    private let stream: AsyncStream<ProgressNotification>
    private let continuation: AsyncStream<ProgressNotification>.Continuation

    /// Valid 8×8 red PNG (75 bytes) returned by `artifactData` for screenshots.
    static let screenshotPNGBase64 =
        "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP4z8CAFWEXHbQSACj/P8Fu7N9hAAAAAElFTkSuQmCC"

    static let screenshotPNGData = Data(base64Encoded: screenshotPNGBase64)!

    static let logExcerpt = (1...40).map {
        "[2026-10-20 18:4\($0 % 10):12.0\($0)] steam-runtime: devkit title heartbeat \($0)"
    }.joined(separator: "\n")

    init(scenario: Scenario) {
        self.scenario = scenario
        (stream, continuation) = AsyncStream.makeStream(of: ProgressNotification.self)

        let paired = Date(timeIntervalSince1970: 1_760_500_000)
        let seen = Date(timeIntervalSince1970: 1_761_000_000)
        let deck = Device(
            name: "steamdeck", address: "192.0.2.10", port: 22, login: "deck",
            family: .deck, pairedAt: paired, addedVia: .explicit, lastSeen: seen)
        let frame = Device(
            name: "steamframe", address: "192.0.2.20", port: 22, login: "deck",
            family: .frame, pairedAt: paired, addedVia: .mdns, lastSeen: seen)

        switch scenario {
        case .empty, .pairingDeclined:
            devices = []
            titles = [:]
            titleDetails = [:]
            jobs = [:]
            artifacts = []
            blobs = [:]
        case .populated, .deployWithProgress:
            devices = [deck, frame]
            let linuxTitle = Title(
                gameid: "zz_devkit_test", registeredWithSteam: true,
                sizeBytes: 41_000_000, modified: seen)
            let protonTitle = Title(
                gameid: "proton_demo", registeredWithSteam: true,
                sizeBytes: 1_200_000_000, modified: seen)
            titles = [
                deck.name: [linuxTitle, protonTitle],
                frame.name: [
                    Title(gameid: "frame_test", registeredWithSteam: nil, sizeBytes: nil, modified: nil)
                ],
            ]
            titleDetails = [
                "\(deck.name)/zz_devkit_test": TitleDetail(
                    gameid: "zz_devkit_test", registeredWithSteam: true,
                    sizeBytes: 41_000_000, modified: seen,
                    config: TitleConfig(
                        entrypoint: "./zz_devkit_test", args: ["--fullscreen"],
                        env: ["DEVKIT_TEST": "1"], runtime: nil,
                        settings: ["steam_play": "0"]),
                    remoteDir: "/home/deck/devkit-game/zz_devkit_test"),
                "\(deck.name)/proton_demo": TitleDetail(
                    gameid: "proton_demo", registeredWithSteam: true,
                    sizeBytes: 1_200_000_000, modified: seen,
                    config: TitleConfig(
                        entrypoint: "game.exe", args: [], env: ["PROTON_LOG": "0"],
                        runtime: .protonExperimental, settings: [:]),
                    remoteDir: "/home/deck/devkit-game/proton_demo"),
            ]
            let shot = MockBridge.screenshotPNGData
            let shotID = "art-shot-1"
            let logsID = "art-logs-1"
            let shotArtifact = Artifact(
                id: shotID, jobId: "job-shot-1", device: deck.name, kind: .screenshot,
                path: "/tmp/mock-devkit/screenshot.png", bytes: shot.count,
                sha256: MockBridge.sha256Hex(shot), mediaType: "image/png",
                remoteSource: "/tmp/devkit-screenshot.png", createdAt: seen)
            let logData = Data(MockBridge.logExcerpt.utf8)
            let logsArtifact = Artifact(
                id: logsID, jobId: "job-logs-1", device: deck.name, kind: .logs,
                path: "/tmp/mock-devkit/steam.log", bytes: logData.count,
                sha256: MockBridge.sha256Hex(logData), mediaType: "text/plain",
                remoteSource: "/home/deck/devkit-utils/steam.log", createdAt: seen)
            artifacts = [
                ArtifactEntry(artifact: shotArtifact, resourceUri: "devkit://artifact/\(shotID)"),
                ArtifactEntry(artifact: logsArtifact, resourceUri: "devkit://artifact/\(logsID)"),
            ]
            blobs = [shotID: shot, logsID: logData]
            jobs = [
                "job-shot-1": JobInfo(
                    id: "job-shot-1", kind: .screenshot, state: .succeeded, device: deck.name,
                    gameid: nil, createdAt: seen, startedAt: seen, finishedAt: seen,
                    timeoutS: 60, progress: nil, code: nil, message: "screenshot captured",
                    artifacts: [shotID]),
                "job-logs-1": JobInfo(
                    id: "job-logs-1", kind: .fetchLogs, state: .succeeded, device: deck.name,
                    gameid: nil, createdAt: seen, startedAt: seen, finishedAt: seen,
                    timeoutS: 120, progress: nil, code: nil, message: "logs fetched",
                    artifacts: [logsID]),
                "job-deploy-1": JobInfo(
                    id: "job-deploy-1", kind: .deploy, state: .succeeded, device: deck.name,
                    gameid: "zz_devkit_test", createdAt: paired, startedAt: paired,
                    finishedAt: seen, timeoutS: 900,
                    progress: JobProgress(
                        phase: "done", bytesDone: 41_000_000, bytesTotal: 41_000_000,
                        itemsDone: 128, rateBps: nil),
                    code: nil, message: "deploy complete", artifacts: []),
            ]
        }
    }

    // MARK: - Test knobs

    /// Force a method to throw (`nil` clears). Keys are protocol method names.
    func injectError(_ method: String, _ error: BridgeError?) {
        locked { injectedErrors[method] = error }
    }

    func clearAllErrors() {
        locked { injectedErrors.removeAll() }
    }

    // MARK: - Internals

    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    private func failIfInjected(_ method: String) throws {
        if let error = locked({ injectedErrors[method] }) { throw error }
    }

    private static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func nextIDs() -> (jobID: String, token: String) {
        locked {
            jobCounter += 1
            tokenCounter += 1
            return ("job-\(jobCounter + 100)", "mock-token-\(tokenCounter)")
        }
    }

    private func mutateJob(_ jobID: String, _ body: (inout JobInfo) -> Void) {
        locked {
            guard var info = jobs[jobID] else { return }
            body(&info)
            jobs[jobID] = info
        }
    }

    private func addArtifact(_ entry: ArtifactEntry, data: Data, jobID: String) {
        locked {
            artifacts.append(entry)
            blobs[entry.artifact.id] = data
            if var info = jobs[jobID] {
                info.artifacts.append(entry.artifact.id)
                jobs[jobID] = info
            }
        }
    }

    /// Runs a short scripted diagnostic job: a couple of progress events, then
    /// success with a fresh artifact.
    private func runScriptedJob(
        jobID: String, token: String, device: String, kind: JobKind, gameid: String?
    ) {
        let delay = eventDelay
        Task.detached { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: delay)
            self.continuation.yield(
                ProgressNotification(progressToken: token, progress: 1, total: 2, message: "capturing"))
            self.mutateJob(jobID) {
                $0.state = .running
                $0.startedAt = Date()
                $0.progress = JobProgress(
                    phase: "capturing", bytesDone: nil, bytesTotal: nil, itemsDone: nil, rateBps: nil)
            }
            try? await Task.sleep(for: delay)
            self.continuation.yield(
                ProgressNotification(progressToken: token, progress: 2, total: 2, message: "done"))
            let artifactID = "art-\(jobID)"
            let artifact: Artifact
            let data: Data
            switch kind {
            case .screenshot:
                data = MockBridge.screenshotPNGData
                artifact = Artifact(
                    id: artifactID, jobId: jobID, device: device, kind: .screenshot,
                    path: "/tmp/mock-devkit/\(artifactID).png", bytes: data.count,
                    sha256: MockBridge.sha256Hex(data), mediaType: "image/png",
                    remoteSource: "/tmp/devkit-screenshot.png", createdAt: Date())
            case .fetchLogs, .controllerConfig:
                data = Data(MockBridge.logExcerpt.utf8)
                let artifactKind: ArtifactKind = kind == .fetchLogs ? .logs : .controllerConfig
                artifact = Artifact(
                    id: artifactID, jobId: jobID, device: device, kind: artifactKind,
                    path: "/tmp/mock-devkit/\(artifactID).log", bytes: data.count,
                    sha256: MockBridge.sha256Hex(data), mediaType: "text/plain",
                    remoteSource: "/home/deck/devkit-utils/steam.log", createdAt: Date())
            case .gpuTrace:
                data = Data("mock gpu trace zip".utf8)
                artifact = Artifact(
                    id: artifactID, jobId: jobID, device: device, kind: .gpuTrace,
                    path: "/tmp/mock-devkit/\(artifactID).zip", bytes: data.count,
                    sha256: MockBridge.sha256Hex(data), mediaType: "application/zip",
                    remoteSource: "/tmp/gpu-trace.zip", createdAt: Date())
            case .rgp:
                data = Data("mock rgp".utf8)
                artifact = Artifact(
                    id: artifactID, jobId: jobID, device: device, kind: .rgp,
                    path: "/tmp/mock-devkit/\(artifactID).rgp", bytes: data.count,
                    sha256: MockBridge.sha256Hex(data), mediaType: "application/octet-stream",
                    remoteSource: "/tmp/capture.rgp", createdAt: Date())
            case .deploy:
                return
            }
            self.addArtifact(ArtifactEntry(
                artifact: artifact, resourceUri: "devkit://artifact/\(artifactID)"), data: data,
                jobID: jobID)
            self.mutateJob(jobID) {
                $0.state = .succeeded
                $0.finishedAt = Date()
                $0.message = "\(kind.rawValue) complete"
                $0.progress = nil
            }
        }
    }

    private func requireDevice(_ selector: String) throws -> Device {
        if let device = locked({ devices.first(where: {
            $0.name == selector || $0.address == selector
        }) }) {
            return device
        }
        throw BridgeError.devkit(DevkitErrorPayload(
            code: ErrorCodes.deviceNotFound,
            message: "no registered device named '\(selector)'",
            details: ["selector": .string(selector)], remoteExitCode: nil))
    }

    private func startJob(kind: JobKind, device: String, gameid: String? = nil) -> JobStart {
        let (jobID, token) = nextIDs()
        locked {
            jobs[jobID] = JobInfo(
                id: jobID, kind: kind, state: .queued, device: device, gameid: gameid,
                createdAt: Date(), startedAt: nil, finishedAt: nil, timeoutS: nil,
                progress: nil, code: nil, message: nil, artifacts: [])
        }
        runScriptedJob(jobID: jobID, token: token, device: device, kind: kind, gameid: gameid)
        return JobStart(jobId: jobID, progressToken: token)
    }

    // MARK: - DevkitBridge

    var progressEvents: AsyncStream<ProgressNotification> { stream }

    func connect() async throws { try failIfInjected("connect") }
    func disconnect() async {}

    func doctor() async throws -> DoctorReport {
        try failIfInjected("doctor")
        return DoctorReport(
            ok: true, context: .bundledApp, platform: "macOS 26.7.1", arch: "arm64",
            pythonVersion: "3.13.7", stateDir: "/Users/dev/Library/Application Support/SteamOSDevkit",
            tools: ToolAvailability(
                rsync: RsyncInfo(
                    path: "/opt/homebrew/bin/rsync", flavor: .rsync, version: "3.4.1",
                    protocolVersion: 32, source: .homebrew, ok: true, message: nil),
                sshPath: "/usr/bin/ssh", sshVersion: "OpenSSH_9.9p1",
                terminal: "Terminal.app", filezilla: nil),
            checks: [
                DoctorCheck(name: "rsync", status: .ok, summary: "rsync 3.4.1 (homebrew)", details: [:]),
                DoctorCheck(name: "ssh", status: .ok, summary: "OpenSSH_9.9p1", details: [:]),
                DoctorCheck(
                    name: "state_dir", status: .ok,
                    summary: "state directory writable",
                    details: ["path": "/Users/dev/Library/Application Support/SteamOSDevkit"]),
                DoctorCheck(
                    name: "local_network", status: .unknown,
                    summary: "local network permission is not programmatically readable",
                    details: [:]),
            ])
    }

    func discover(timeoutSeconds: Int) async throws -> [DiscoveredDevice] {
        try failIfInjected("discover")
        switch scenario {
        case .empty:
            return []
        case .pairingDeclined, .populated, .deployWithProgress:
            let deck = DiscoveredDevice(
                name: "steamdeck", address: "192.0.2.10", port: 22, login: "deck",
                sshd: true, via: .mdns)
            if scenario == .pairingDeclined { return [deck] }
            return [
                deck,
                DiscoveredDevice(
                    name: "steamframe", address: "192.0.2.20", port: 22, login: "deck",
                    sshd: true, via: .mdns),
            ]
        }
    }

    func listDevices() async throws -> [Device] {
        try failIfInjected("listDevices")
        return locked { devices }
    }

    func forgetDevice(_ device: String) async throws -> DevkitResult {
        try failIfInjected("forgetDevice")
        let entry = try requireDevice(device)
        locked { devices.removeAll { $0.name == entry.name } }
        return DevkitResult(
            ok: true, code: nil, device: entry, message: "forgot \(entry.name)",
            uploadComplete: nil, registrationComplete: nil, launch: nil)
    }

    func pair(device: String, timeoutSeconds: Int) async throws -> DevkitResult {
        try failIfInjected("pair")
        if scenario == .pairingDeclined {
            throw BridgeError.devkit(DevkitErrorPayload(
                code: ErrorCodes.pairingDeclined,
                message: "pairing declined — the request was rejected on the device",
                details: ["device": .string(device)], remoteExitCode: 403))
        }
        // Resolve a discovered name, or accept an explicit IP/hostname.
        let discovered = try await discover(timeoutSeconds: 5)
        let name = discovered.first(where: {
            $0.name == device || $0.address == device
        })?.name ?? device
        let address = discovered.first(where: { $0.name == name })?.address ?? device
        if let existing = locked({ devices.first(where: { $0.name == name }) }) {
            return DevkitResult(
                ok: true, code: nil, device: existing, message: "paired",
                uploadComplete: nil, registrationComplete: nil, launch: nil)
        }
        let entry = Device(
            name: name, address: address, port: 22, login: "deck",
            family: name.contains("frame") ? .frame : .deck,
            pairedAt: Date(), addedVia: address == device ? .explicit : .mdns, lastSeen: Date())
        locked { devices.append(entry) }
        return DevkitResult(
            ok: true, code: nil, device: entry, message: "paired",
            uploadComplete: nil, registrationComplete: nil, launch: nil)
    }

    func status(device: String) async throws -> DeviceStatus {
        try failIfInjected("status")
        let entry = try requireDevice(device)
        let steamStatus = locked { steamModes[entry.name] } ?? (entry.family == .frame ? SteamStatus.osDev : SteamStatus.os)
        if entry.family == .frame {
            return DeviceStatus(
                hostname: entry.name, osName: "SteamOS", osVersion: "3.8.28",
                isDeckard: true, sessionStatus: "plasma-wayland", steamStatus: steamStatus,
                steamOsclientBranch: "stable", steamOsclientVersion: .int(1_791_918_634),
                cefDebuggingEnabled: false, renderdocLayerEnabled: false,
                renderdocReplayServerRunning: false, userPasswordIsSet: true,
                steamDefaultArgs: [], steamCurrentArgs: [], steamLaunchFlags: [:], raw: [:])
        }
        return DeviceStatus(
            hostname: entry.name, osName: "SteamOS", osVersion: "3.8.28",
            isDeckard: false, sessionStatus: "gamescope", steamStatus: steamStatus,
            steamOsclientBranch: "stable", steamOsclientVersion: .int(1_791_918_634),
            cefDebuggingEnabled: true, renderdocLayerEnabled: false,
            renderdocReplayServerRunning: false, userPasswordIsSet: true,
            steamDefaultArgs: ["-steamdeck"], steamCurrentArgs: ["-steamdeck"],
            steamLaunchFlags: ["-steamdeck": "default"], raw: [:])
    }

    func capabilities(device: String?) async throws -> Capabilities {
        try failIfInjected("capabilities")
        let host = HostCapabilities(
            platform: "macOS 26.7.1", arch: "arm64", context: .bundledApp,
            pythonVersion: "3.13.7",
            stateDir: "/Users/dev/Library/Application Support/SteamOSDevkit",
            stateDirWritable: true,
            rsync: RsyncInfo(
                path: "/opt/homebrew/bin/rsync", flavor: .rsync, version: "3.4.1",
                protocolVersion: 32, source: .homebrew, ok: true, message: nil),
            sshPath: "/usr/bin/ssh", sshVersion: "OpenSSH_9.9p1",
            terminal: "Terminal.app", localNetwork: .likelyOk)
        var deviceCaps: DeviceCapabilities?
        var ops: [String: OperationCapability] = [:]
        func add(_ name: String, _ status: OperationStatus, reason: String? = nil, dependency: String? = nil) {
            ops[name] = OperationCapability(status: status, reason: reason, dependency: dependency)
        }
        for name in [
            "doctor", "discover", "list_devices", "forget_device", "capabilities",
            "job_status", "job_cancel", "list_jobs", "list_artifacts", "read_artifact",
            "migrate", "setup", "version", "shell",
            "configure_title", "preview_deploy", "register_title", "delete_title",
            "controller_config", "perf_overlay", "frametimes", "renderdoc_layer",
            "renderdoc_server", "proton_logging", "set_steam_mode", "set_session",
            "reload_steam", "inspect_title", "list_titles",
        ] {
            add(name, .supportedUntested)
        }
        // Green in docs/hardware-matrix.md run 1 — the only ops that may claim tested.
        for name in ["pair", "status", "deploy", "launch_title", "fetch_logs", "screenshot"] {
            add(name, .supportedTested)
        }
        if let selector = device, let entry = try requireDevice(selector) as Device? {
            let isFrame = entry.family == .frame
            deviceCaps = DeviceCapabilities(
                family: entry.family, osVersion: "3.8.28", steamBranch: "stable",
                steamVersion: 1_791_918_634, hasGpuTrace: !isFrame,
                gamescopeVersion: isFrame ? nil : "3.16.17",
                renderdocLayerEnabled: false, renderdocReplayServerRunning: false,
                cefDebuggingEnabled: !isFrame, hasMangohudctl: true,
                sessionSelect: .steamos)
            if isFrame {
                add("gpu_trace", .unavailableDevice, reason: "gpu-trace/RGP capture is Deck-only upstream")
                add("rgp_capture", .unavailableDevice, reason: "gpu-trace/RGP capture is Deck-only upstream")
            } else {
                add("gpu_trace", .supportedUntested)
                add("rgp_capture", .supportedUntested)
            }
        } else {
            add("gpu_trace", .supportedUntested)
            add("rgp_capture", .supportedUntested)
        }
        add(
            "vs_remote_debug", .unavailableHost,
            reason: "msvsmon deployment is a Windows-host-only upstream feature")
        let unsupported = "the upstream SteamOS devkit protocol has no such operation"
        add("stop_title", .unsupportedUpstream, reason: unsupported)
        add("launch_readiness", .unsupportedUpstream, reason: unsupported)
        add("title_stdout_streaming", .unsupportedUpstream, reason: unsupported)
        return Capabilities(host: host, device: deviceCaps, operations: ops)
    }

    func listTitles(device: String) async throws -> [Title] {
        try failIfInjected("listTitles")
        let entry = try requireDevice(device)
        return locked { titles[entry.name] } ?? []
    }

    func inspectTitle(device: String, gameid: String) async throws -> TitleDetail {
        try failIfInjected("inspectTitle")
        let entry = try requireDevice(device)
        if let detail = locked({ titleDetails["\(entry.name)/\(gameid)"] }) { return detail }
        throw BridgeError.devkit(DevkitErrorPayload(
            code: ErrorCodes.validation,
            message: "no devkit title '\(gameid)' on \(entry.name)",
            details: nil, remoteExitCode: nil))
    }

    func configureTitle(device: String, gameid: String, config: TitleConfig) async throws -> DevkitResult {
        try failIfInjected("configureTitle")
        let entry = try requireDevice(device)
        let key = "\(entry.name)/\(gameid)"
        try locked {
            guard var detail = titleDetails[key] else {
                throw BridgeError.devkit(DevkitErrorPayload(
                    code: ErrorCodes.validation,
                    message: "no devkit title '\(gameid)' on \(entry.name)",
                    details: nil, remoteExitCode: nil))
            }
            // H1 merge semantics: nil entrypoint / empty collections leave values.
            if let entrypoint = config.entrypoint { detail.config.entrypoint = entrypoint }
            if !config.args.isEmpty { detail.config.args = config.args }
            if !config.env.isEmpty { detail.config.env = config.env }
            if let runtime = config.runtime { detail.config.runtime = runtime }
            if !config.settings.isEmpty { detail.config.settings = config.settings }
            titleDetails[key] = detail
        }
        return DevkitResult(
            ok: true, code: nil, device: entry, message: "title \(gameid) configured",
            uploadComplete: nil, registrationComplete: nil, launch: nil)
    }

    func previewDeploy(
        device: String, gameid: String, sourceDir: String, filters: FilterSpec?, clean: Bool
    ) async throws -> DeployPreview {
        try failIfInjected("previewDeploy")
        _ = try requireDevice(device)
        var warnings: [String] = []
        if clean { warnings.append("clean is destructive: remote-only files will be deleted") }
        return DeployPreview(
            filesAdded: 12, filesChanged: 3, filesDeleted: clean ? 2 : 0,
            bytesToTransfer: 41_000_000,
            sampleChanges: ["+ bin/game", "~ assets/pak01.vpk", "~ run.sh"] + (clean ? ["- old/readme.txt"] : []),
            warnings: warnings)
    }

    func deploy(_ spec: DeploySpec) async throws -> JobStart {
        try failIfInjected("deploy")
        _ = try requireDevice(spec.device)
        let (jobID, token) = nextIDs()
        locked {
            jobs[jobID] = JobInfo(
                id: jobID, kind: .deploy, state: .queued, device: spec.device,
                gameid: spec.gameid, createdAt: Date(), startedAt: nil, finishedAt: nil,
                timeoutS: 900, progress: nil, code: nil, message: nil, artifacts: [])
        }
        let scripted = scenario == .deployWithProgress
        let total = 10
        let delay = eventDelay
        Task.detached { [weak self] in
            guard let self else { return }
            if scripted {
                for i in 1...total {
                    try? await Task.sleep(for: delay)
                    let phase = i < total ? "uploading" : "finishing"
                    self.continuation.yield(ProgressNotification(
                        progressToken: token, progress: Double(i), total: Double(total),
                        message: phase))
                    self.mutateJob(jobID) {
                        $0.state = .running
                        $0.startedAt = $0.startedAt ?? Date()
                        $0.progress = JobProgress(
                            phase: phase, bytesDone: i * 4_100_000, bytesTotal: 41_000_000,
                            itemsDone: i * 13, rateBps: 20_500_000)
                    }
                }
            } else {
                try? await Task.sleep(for: delay)
                self.continuation.yield(ProgressNotification(
                    progressToken: token, progress: 1, total: 1, message: "done"))
            }
            self.mutateJob(jobID) {
                $0.state = .succeeded
                $0.finishedAt = Date()
                $0.message = "deploy complete"
                $0.progress = nil
            }
            if spec.register {
                let name = self.locked { self.devices.first { $0.name == spec.device }?.name } ?? spec.device
                self.locked {
                    if self.titles[name]?.contains(where: { $0.gameid == spec.gameid }) != true {
                        var list = self.titles[name] ?? []
                        list.append(Title(
                            gameid: spec.gameid, registeredWithSteam: true,
                            sizeBytes: 41_000_000, modified: Date()))
                        self.titles[name] = list
                    }
                    let key = "\(name)/\(spec.gameid)"
                    if self.titleDetails[key] == nil {
                        self.titleDetails[key] = TitleDetail(
                            gameid: spec.gameid, registeredWithSteam: true,
                            sizeBytes: 41_000_000, modified: Date(),
                            config: spec.config ?? TitleConfig(),
                            remoteDir: "/home/deck/devkit-game/\(spec.gameid)")
                    }
                }
            }
        }
        return JobStart(jobId: jobID, progressToken: token)
    }

    func registerTitle(device: String, gameid: String) async throws -> DevkitResult {
        try failIfInjected("registerTitle")
        let entry = try requireDevice(device)
        return DevkitResult(
            ok: true, code: nil, device: entry,
            message: "title \(gameid) registered with Steam",
            uploadComplete: nil, registrationComplete: true, launch: nil)
    }

    func launchTitle(device: String, gameid: String) async throws -> LaunchResult {
        try failIfInjected("launchTitle")
        _ = try requireDevice(device)
        return LaunchResult(
            gameid: gameid, launched: true,
            deviceResponse: "run-game accepted", readiness: "unknown")
    }

    func deleteTitle(device: String, gameid: String) async throws -> DevkitResult {
        try failIfInjected("deleteTitle")
        let entry = try requireDevice(device)
        try locked {
            guard titles[entry.name]?.contains(where: { $0.gameid == gameid }) == true else {
                throw BridgeError.devkit(DevkitErrorPayload(
                    code: ErrorCodes.validation,
                    message: "no devkit title '\(gameid)' on \(entry.name)",
                    details: nil, remoteExitCode: nil))
            }
            titles[entry.name]?.removeAll { $0.gameid == gameid }
            titleDetails.removeValue(forKey: "\(entry.name)/\(gameid)")
        }
        return DevkitResult(
            ok: true, code: nil, device: entry, message: "deleted \(gameid)",
            uploadComplete: nil, registrationComplete: nil, launch: nil)
    }

    func fetchLogs(device: String, kinds: [LogKind]) async throws -> JobStart {
        try failIfInjected("fetchLogs")
        let entry = try requireDevice(device)
        return startJob(kind: .fetchLogs, device: entry.name)
    }

    func screenshot(device: String, mode: ScreenshotMode) async throws -> JobStart {
        try failIfInjected("screenshot")
        let entry = try requireDevice(device)
        return startJob(kind: .screenshot, device: entry.name)
    }

    func controllerConfig(device: String, gameid: String) async throws -> JobStart {
        try failIfInjected("controllerConfig")
        let entry = try requireDevice(device)
        return startJob(kind: .controllerConfig, device: entry.name, gameid: gameid)
    }

    func gpuTrace(device: String) async throws -> JobStart {
        try failIfInjected("gpuTrace")
        let entry = try requireDevice(device)
        return startJob(kind: .gpuTrace, device: entry.name)
    }

    func rgpCapture(device: String) async throws -> JobStart {
        try failIfInjected("rgpCapture")
        let entry = try requireDevice(device)
        return startJob(kind: .rgp, device: entry.name)
    }

    func perfOverlay(device: String, enabled: Bool) async throws -> DevkitResult {
        try failIfInjected("perfOverlay")
        let entry = try requireDevice(device)
        return ok(entry, "performance overlay \(enabled ? "enabled" : "disabled")")
    }

    func frametimes(device: String, enabled: Bool) async throws -> DevkitResult {
        try failIfInjected("frametimes")
        let entry = try requireDevice(device)
        return ok(entry, "frametimes capture \(enabled ? "enabled" : "disabled")")
    }

    func renderdocLayer(device: String, enabled: Bool) async throws -> DevkitResult {
        try failIfInjected("renderdocLayer")
        let entry = try requireDevice(device)
        return ok(entry, "renderdoc layer \(enabled ? "enabled" : "disabled")")
    }

    func renderdocServer(device: String, action: RenderdocServerAction) async throws -> DevkitResult {
        try failIfInjected("renderdocServer")
        let entry = try requireDevice(device)
        let message: String
        switch action {
        case .start: message = "renderdoc replay server started"
        case .stop: message = "renderdoc replay server stopped"
        case .status: message = "renderdoc replay server: stopped"
        }
        return ok(entry, message)
    }

    func protonLogging(device: String, mode: ProtonLogMode, winedebug: String?) async throws -> DevkitResult {
        try failIfInjected("protonLogging")
        let entry = try requireDevice(device)
        return ok(entry, "proton logging \(mode == .on ? "on" : "off")")
    }

    func setSteamMode(
        device: String, mode: SteamMode, gameid: String?, args: String?, gdbserver: Bool
    ) async throws -> DevkitResult {
        try failIfInjected("setSteamMode")
        let entry = try requireDevice(device)
        locked { steamModes[entry.name] = mode == .os ? .os : .osDev }
        return ok(entry, "steam mode set to \(mode.rawValue)")
    }

    func setSession(device: String, session: DeviceSession) async throws -> DevkitResult {
        try failIfInjected("setSession")
        let entry = try requireDevice(device)
        return ok(entry, "session set to \(session.rawValue) (takes effect after steam reload)")
    }

    func reloadSteam(device: String) async throws -> DevkitResult {
        try failIfInjected("reloadSteam")
        let entry = try requireDevice(device)
        return ok(entry, "steam reloading on \(entry.name)")
    }

    private func ok(_ device: Device, _ message: String) -> DevkitResult {
        DevkitResult(
            ok: true, code: nil, device: device, message: message,
            uploadComplete: nil, registrationComplete: nil, launch: nil)
    }

    func jobStatus(jobId: String) async throws -> JobInfo {
        try failIfInjected("jobStatus")
        if let info = locked({ jobs[jobId] }) { return info }
        throw BridgeError.devkit(DevkitErrorPayload(
            code: ErrorCodes.jobNotFound, message: "no job '\(jobId)'",
            details: nil, remoteExitCode: nil))
    }

    func jobCancel(jobId: String) async throws -> DevkitResult {
        try failIfInjected("jobCancel")
        try locked {
            guard var info = jobs[jobId] else {
                throw BridgeError.devkit(DevkitErrorPayload(
                    code: ErrorCodes.jobNotFound, message: "no job '\(jobId)'",
                    details: nil, remoteExitCode: nil))
            }
            if !info.state.isTerminal {
                info.state = .cancelled
                info.finishedAt = Date()
                info.message = "cancelled"
                jobs[jobId] = info
            }
        }
        return DevkitResult(
            ok: true, code: nil, device: nil, message: "job \(jobId) cancelled",
            uploadComplete: nil, registrationComplete: nil, launch: nil)
    }

    func listJobs(device: String?, state: JobState?) async throws -> [JobInfo] {
        try failIfInjected("listJobs")
        return locked {
            jobs.values
                .filter { device == nil || $0.device == device }
                .filter { state == nil || $0.state == state }
                .sorted { $0.createdAt < $1.createdAt }
        }
    }

    func listArtifacts(device: String?, kind: ArtifactKind?) async throws -> [ArtifactEntry] {
        try failIfInjected("listArtifacts")
        return locked {
            artifacts
                .filter { device == nil || $0.artifact.device == device }
                .filter { kind == nil || $0.artifact.kind == kind }
                .sorted { $0.artifact.createdAt > $1.artifact.createdAt }
        }
    }

    func readArtifact(artifactId: String, tailLines: Int) async throws -> ArtifactRead {
        try failIfInjected("readArtifact")
        guard let entry = locked({ artifacts.first(where: { $0.artifact.id == artifactId }) }) else {
            throw BridgeError.devkit(DevkitErrorPayload(
                code: ErrorCodes.artifactNotFound, message: "no artifact '\(artifactId)'",
                details: nil, remoteExitCode: nil))
        }
        let isText = entry.artifact.mediaType?.hasPrefix("text/") == true
            || entry.artifact.kind == .logs || entry.artifact.kind == .controllerConfig
        let excerpt: String?
        if isText, let data = locked({ blobs[artifactId] }) {
            let lines = String(decoding: data, as: UTF8.self).components(separatedBy: "\n")
            excerpt = lines.suffix(tailLines).joined(separator: "\n")
        } else {
            excerpt = nil
        }
        return ArtifactRead(
            artifactId: artifactId, kind: entry.artifact.kind.rawValue,
            mediaType: entry.artifact.mediaType, resourceUri: entry.resourceUri,
            excerpt: excerpt)
    }

    func artifactData(artifactId: String) async throws -> ArtifactPayload {
        try failIfInjected("artifactData")
        guard let entry = locked({ artifacts.first(where: { $0.artifact.id == artifactId }) }) else {
            throw BridgeError.devkit(DevkitErrorPayload(
                code: ErrorCodes.artifactNotFound, message: "no artifact '\(artifactId)'",
                details: nil, remoteExitCode: nil))
        }
        let data = locked { blobs[artifactId] } ?? Data()
        return ArtifactPayload(
            data: data, mimeType: entry.artifact.mediaType ?? "application/octet-stream")
    }
}

// MARK: - Preview helpers

extension AppState {
    /// App state backed by a MockBridge scenario, for #Preview and tests.
    static func preview(scenario: MockBridge.Scenario = .populated) -> AppState {
        AppState(bridge: MockBridge(scenario: scenario))
    }
}
