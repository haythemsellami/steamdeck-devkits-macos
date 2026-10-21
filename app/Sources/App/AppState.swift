import Foundation
import SwiftUI

// AppState.swift — the app-wide model. Owns the bridge, the device registry
// snapshot, job state (polled + pushed), artifact/doctor caches, navigation
// selection for deep links, and the global banner.
//
// Progress has two sources, merged in favour of push:
//   1. ONE subscription task on `bridge.progressEvents`, routed by
//      progressToken → jobId (registered when the UI starts a job).
//   2. A 1 s `jobStatus` poll for every non-terminal job, so progress still
//      works for jobs not started in this session (survives app restart).

/// Sidebar/detail navigation target. `.job` is set programmatically by
/// deep links (deploy start, job rows); it has no permanent sidebar row.
enum SidebarItem: Hashable {
    case devices
    case device(String)
    case titles(String)
    case jobs
    case job(String)
    case artifacts
    case doctor
}

@MainActor
@Observable
final class AppState {
    let bridge: any DevkitBridge

    var devices: [Device] = []
    /// Reachability per device name: nil = not probed this session.
    var reachability: [String: Bool] = [:]
    var jobs: [String: JobInfo] = [:]
    /// Latest push progress per job id; wins over polled `JobInfo.progress`.
    var liveProgress: [String: ProgressNotification] = [:]
    var artifacts: [ArtifactEntry] = []
    var doctorReport: DoctorReport?
    var hostLocalNetwork: LocalNetworkInference?

    var banner: BannerModel?
    var selection: SidebarItem? = .devices
    var highlightedArtifactID: String?
    var showsDiscoverSheet = false
    var showsDeploySheet = false

    private var tokenToJob: [String: String] = [:]
    private var progressTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?

    init(bridge: any DevkitBridge) {
        self.bridge = bridge
    }

    static func makeForProduction() -> AppState {
        AppState(bridge: BridgeSetup.makeProductionBridge())
    }

    /// Called once from the root view's `.task`. Idempotent.
    func start() {
        guard progressTask == nil else { return }
        progressTask = Task { [weak self] in
            guard let self else { return }
            for await event in bridge.progressEvents {
                guard let jobID = tokenToJob[event.progressToken] else { continue }
                liveProgress[jobID] = event
            }
        }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                try? await Task.sleep(for: .seconds(1))
                let activeIDs = jobs.values.filter { !$0.state.isTerminal }.map(\.id)
                for jobID in activeIDs {
                    await refreshJob(jobID)
                }
            }
        }
    }

    /// Registers a freshly started job so its progressToken routes here.
    func registerJob(_ start: JobStart) {
        tokenToJob[start.progressToken] = start.jobId
        Task { await refreshJob(start.jobId) }
    }

    func refreshJob(_ jobID: String) async {
        if let info = try? await bridge.jobStatus(jobId: jobID) {
            jobs[jobID] = info
        }
    }

    /// Effective progress for one job: push wins for the token it covers,
    /// falling back to the polled JobInfo.progress.
    func effectiveProgress(for jobID: String) -> (fraction: Double?, phase: String?, rateBps: Double?) {
        if let push = liveProgress[jobID] {
            let fraction = push.total.map { $0 > 0 ? push.progress / $0 : 0 }
            return (fraction, push.message, jobs[jobID]?.progress?.rateBps)
        }
        if let progress = jobs[jobID]?.progress {
            let fraction: Double?
            if let done = progress.bytesDone, let total = progress.bytesTotal, total > 0 {
                fraction = Double(done) / Double(total)
            } else {
                fraction = nil
            }
            return (fraction, progress.phase, progress.rateBps)
        }
        return (nil, nil, nil)
    }

    func refreshDevices() async {
        do {
            devices = try await bridge.listDevices()
        } catch {
            present(error)
        }
    }

    func refreshArtifacts() async {
        do {
            artifacts = try await bridge.listArtifacts()
        } catch {
            present(error)
        }
    }

    func refreshDoctor() async {
        do {
            doctorReport = try await bridge.doctor()
            hostLocalNetwork = try await bridge.capabilities().host.localNetwork
        } catch {
            present(error)
        }
    }

    func markReachable(_ device: String, _ reachable: Bool) {
        reachability[device] = reachable
    }

    func present(_ error: Error) {
        if let model = ErrorPresentation.model(for: error) {
            banner = model
        }
    }

    func presentInfo(_ message: String) {
        banner = .info(message)
    }

    func dismissBanner() {
        banner = nil
    }

    // MARK: - Deep links

    func showJob(_ jobID: String) {
        selection = .job(jobID)
    }

    func showArtifact(_ artifactID: String) {
        highlightedArtifactID = artifactID
        selection = .artifacts
    }

    func showDoctor() {
        selection = .doctor
    }

    /// The device the deploy sheet / titles screens default to.
    var activeDeviceName: String? {
        if case .device(let name) = selection { return name }
        if case .titles(let name) = selection { return name }
        return devices.first?.name
    }
}
