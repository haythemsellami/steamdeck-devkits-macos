import SwiftUI

// JobsView.swift — all jobs across devices: table with kind/state/duration,
// live progress for active jobs, cancel for non-terminal. listJobs on appear
// plus 1 s polling while anything is non-terminal; push progress (AppState)
// wins over polled progress for the token it covers.

@MainActor
@Observable
final class JobsViewModel {
    let bridge: any DevkitBridge
    let appState: AppState

    var error: BannerModel?
    var selectedJobID: String?
    var isLoading = false

    private var polledJobs: [String: JobInfo] = [:]

    init(bridge: any DevkitBridge, appState: AppState) {
        self.bridge = bridge
        self.appState = appState
    }

    /// listJobs results merged with AppState's jobStatus cache (which may be
    /// newer for just-started jobs), oldest first like `devkit jobs`.
    var jobs: [JobInfo] {
        var merged = polledJobs
        for (id, info) in appState.jobs where merged[id] == nil {
            merged[id] = info
        }
        return merged.values.sorted { $0.createdAt < $1.createdAt }
    }

    var hasActiveJobs: Bool {
        jobs.contains { !$0.state.isTerminal }
    }

    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let list = try await bridge.listJobs(device: nil, state: nil)
            polledJobs = Dictionary(list.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
            error = nil
        } catch {
            self.error = ErrorPresentation.model(for: error)
        }
    }

    /// Polls every second while the view is alive; only hits the bridge when
    /// something is still running.
    func pollWhileActive() async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
            if hasActiveJobs {
                await refresh()
            }
        }
    }

    func cancel(_ jobID: String) async {
        do {
            _ = try await bridge.jobCancel(jobId: jobID)
            await appState.refreshJob(jobID)
            await refresh()
        } catch let failure {
            error = ErrorPresentation.model(for: failure)
        }
    }

    func progress(for jobID: String) -> (fraction: Double?, phase: String?, rateBps: Double?) {
        appState.effectiveProgress(for: jobID)
    }
}

struct JobsView: View {
    @Environment(AppState.self) private var appState
    @State private var model: JobsViewModel?

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Loading jobs…")
            }
        }
        .navigationTitle("Jobs")
        .task {
            if model == nil {
                let vm = JobsViewModel(bridge: appState.bridge, appState: appState)
                model = vm
                await vm.refresh()
            }
        }
        .task {
            await model?.pollWhileActive()
        }
    }

    @ViewBuilder
    private func content(_ model: JobsViewModel) -> some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            if let error = model.error {
                ErrorBanner(model: error) { model.error = nil }
                    .padding([.horizontal, .top])
            }
            if model.jobs.isEmpty {
                ContentUnavailableView(
                    "No jobs yet",
                    systemImage: "list.bullet.rectangle",
                    description: Text("Deploys, log fetches, screenshots, and captures show up here."))
            } else {
                table(model)
            }
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button(action: { Task { await model.refresh() } }) {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
            }
        }
    }

    @ViewBuilder
    private func table(_ model: JobsViewModel) -> some View {
        @Bindable var model = model
        Table(model.jobs, selection: $model.selectedJobID) {
            TableColumn("") { job in
                JobKindIcon(kind: job.kind)
            }
            .width(24)
            TableColumn("job") { job in
                Text(job.id)
                    .font(.system(.callout, design: .monospaced))
            }
            TableColumn("device") { job in
                Text(job.device ?? "—")
            }
            .width(110)
            TableColumn("gameid") { job in
                Text(job.gameid ?? "—")
                    .font(.system(.callout, design: .monospaced))
            }
            TableColumn("state") { job in
                StateBadge(state: job.state)
            }
            .width(90)
            TableColumn("created") { job in
                Text(Format.date(job.createdAt))
                    .foregroundStyle(.secondary)
            }
            .width(140)
            TableColumn("duration") { job in
                Text(Format.duration(of: job))
                    .foregroundStyle(.secondary)
            }
            .width(70)
            TableColumn("progress") { job in
                if !job.state.isTerminal {
                    HStack(spacing: 6) {
                        if let fraction = model.progress(for: job.id).fraction {
                            ProgressView(value: fraction)
                                .frame(width: 80)
                        } else {
                            ProgressView()
                                .progressViewStyle(.linear)
                                .frame(width: 80)
                        }
                        Text(model.progress(for: job.id).phase ?? "")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } else if let message = job.message {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
        .contextMenu(forSelectionType: JobInfo.ID.self) { selection in
            if let jobID = selection.first {
                Button("Open") { appState.showJob(jobID) }
                if let job = model.jobs.first(where: { $0.id == jobID }), !job.state.isTerminal {
                    Button("Cancel job", role: .destructive) {
                        Task { await model.cancel(jobID) }
                    }
                }
            }
        } primaryAction: { selection in
            if let jobID = selection.first {
                appState.showJob(jobID)
            }
        }
    }
}

struct JobsView_Previews: PreviewProvider {
    static var previews: some View {
        JobsView()
            .environment(AppState.preview())
    }
}
