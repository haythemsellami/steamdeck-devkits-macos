import SwiftUI

// JobDetailView.swift — one job's live view: progress bar (determinate when
// a total is known, else indeterminate), phase label, transfer rate, cancel,
// and artifact chips on completion that deep-link into Artifacts.

@MainActor
@Observable
final class JobDetailViewModel {
    let bridge: any DevkitBridge
    let appState: AppState
    let jobID: String

    var isCancelling = false
    var error: BannerModel?

    init(bridge: any DevkitBridge, appState: AppState, jobID: String) {
        self.bridge = bridge
        self.appState = appState
        self.jobID = jobID
    }

    /// Always the freshest snapshot AppState holds (polled + pushed).
    var info: JobInfo? {
        appState.jobs[jobID]
    }

    var progress: (fraction: Double?, phase: String?, rateBps: Double?) {
        appState.effectiveProgress(for: jobID)
    }

    func loadIfNeeded() async {
        if appState.jobs[jobID] == nil {
            await appState.refreshJob(jobID)
            if appState.jobs[jobID] == nil {
                error = ErrorPresentation.model(for: DevkitErrorPayload(
                    code: ErrorCodes.jobNotFound,
                    message: "no job '\(jobID)' known to the server",
                    details: nil, remoteExitCode: nil))
            }
        }
        await appState.refreshArtifacts()
    }

    func cancel() async {
        guard !isCancelling else { return }
        isCancelling = true
        defer { isCancelling = false }
        do {
            _ = try await bridge.jobCancel(jobId: jobID)
            await appState.refreshJob(jobID)
        } catch let failure {
            if let model = ErrorPresentation.model(for: failure) {
                error = model
            }
        }
    }
}

struct JobDetailView: View {
    @Environment(AppState.self) private var appState
    @State private var model: JobDetailViewModel?

    let jobID: String

    init(jobID: String) {
        self.jobID = jobID
    }

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Loading job…")
            }
        }
        .navigationTitle("Job \(jobID)")
        .task {
            if model == nil {
                let vm = JobDetailViewModel(bridge: appState.bridge, appState: appState, jobID: jobID)
                model = vm
                await vm.loadIfNeeded()
            }
        }
    }

    @ViewBuilder
    private func content(_ model: JobDetailViewModel) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            if let error = model.error {
                ErrorBanner(model: error) { model.error = nil }
            }
            if let info = model.info {
                header(info)
                progressSection(model, info)
                if let message = info.message {
                    Text(message)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                if info.state.isTerminal && !info.artifacts.isEmpty {
                    artifactsSection(info)
                }
                Spacer()
            } else {
                ProgressView("Fetching job status…")
                Spacer()
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func header(_ info: JobInfo) -> some View {
        HStack(spacing: 10) {
            JobKindIcon(kind: info.kind)
            Text(info.kind.rawValue.replacingOccurrences(of: "_", with: " "))
                .font(.title3.weight(.semibold))
            StateBadge(state: info.state)
            Spacer()
            if let device = info.device {
                Label(device, systemImage: "gamecontroller")
                    .foregroundStyle(.secondary)
            }
            if let gameid = info.gameid {
                Text(gameid)
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            if !info.state.isTerminal {
                Button(action: { Task { await model?.cancel() } }) {
                    if model?.isCancelling == true {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("Cancel")
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func progressSection(_ model: JobDetailViewModel, _ info: JobInfo) -> some View {
        if !info.state.isTerminal {
            VStack(alignment: .leading, spacing: 6) {
                if let fraction = model.progress.fraction {
                    ProgressView(value: fraction)
                } else {
                    ProgressView()
                        .progressViewStyle(.linear)
                }
                HStack {
                    Text(model.progress.phase ?? info.state.rawValue)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if let rate = model.progress.rateBps {
                        Text(Format.rate(rate))
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    if let fraction = model.progress.fraction {
                        Text("\(Int((fraction * 100).rounded()))%")
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: 520)
        }
    }

    @ViewBuilder
    private func artifactsSection(_ info: JobInfo) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Artifacts")
                .font(.headline)
            HStack(spacing: 8) {
                ForEach(info.artifacts, id: \.self) { artifactID in
                    Button(action: { appState.showArtifact(artifactID) }) {
                        HStack(spacing: 6) {
                            if let entry = appState.artifacts.first(where: { $0.id == artifactID }) {
                                ArtifactKindIcon(kind: entry.artifact.kind)
                                Text(entry.artifact.kind.rawValue)
                            } else {
                                Image(systemName: "doc")
                                Text(artifactID)
                            }
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.accentColor.opacity(0.12), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .help("Open in Artifacts")
                }
            }
        }
    }
}

struct JobDetailView_Previews: PreviewProvider {
    static var previews: some View {
        JobDetailView(jobID: "job-deploy-1")
            .environment(AppState.preview(scenario: .deployWithProgress))
    }
}
