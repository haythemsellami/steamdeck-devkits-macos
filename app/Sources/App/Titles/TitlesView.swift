import SwiftUI

// TitlesView.swift — per-device devkit titles: table, refresh, context menu
// and detail-pane actions (launch, configure, fetch logs, screenshot,
// controller config, delete). Job-starting actions register with AppState
// and navigate to the job; delete requires typing the gameid.

/// Table/sheet rows need Identifiable; Title isn't (contract), so wrap it.
struct TitleRow: Identifiable, Equatable {
    let title: Title
    var id: String { title.gameid }
}

@MainActor
@Observable
final class TitlesViewModel {
    let bridge: any DevkitBridge
    let appState: AppState
    let deviceName: String

    var titles: [Title] = []
    var isLoading = false
    var loadError: BannerModel?
    var selectedGameID: String?
    var detail: TitleDetail?
    var actionOutcome: BannerModel?
    var runningAction: String?
    var titlePendingDeletion: Title?
    var deleteConfirmationText = ""
    var configuringGameID: String?

    init(bridge: any DevkitBridge, appState: AppState, deviceName: String) {
        self.bridge = bridge
        self.appState = appState
        self.deviceName = deviceName
    }

    var rows: [TitleRow] {
        titles.map(TitleRow.init)
    }

    var deleteConfirmed: Bool {
        deleteConfirmationText == titlePendingDeletion?.gameid
    }

    func refresh() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            titles = try await bridge.listTitles(device: deviceName)
            loadError = nil
        } catch {
            if let model = ErrorPresentation.model(for: error) {
                loadError = model
            }
        }
    }

    func select(_ gameid: String?) async {
        selectedGameID = gameid
        guard let gameid else {
            detail = nil
            return
        }
        detail = try? await bridge.inspectTitle(device: deviceName, gameid: gameid)
    }

    private func run(_ name: String, _ operation: () async throws -> Void) async {
        guard runningAction == nil else { return }
        runningAction = name
        defer { runningAction = nil }
        do {
            try await operation()
        } catch {
            if let model = ErrorPresentation.model(for: error) {
                actionOutcome = model
            }
        }
    }

    private func startJob(_ name: String, _ operation: () async throws -> JobStart) async {
        await run(name) {
            let start = try await operation()
            appState.registerJob(start)
            actionOutcome = .info("\(name) job started")
            appState.showJob(start.jobId)
        }
    }

    func launch(_ gameid: String) async {
        await run("launch") {
            let result = try await bridge.launchTitle(device: deviceName, gameid: gameid)
            actionOutcome = .info(
                result.launched
                    ? "launch accepted by the device (readiness unknown)"
                    : "launch not accepted: \(result.deviceResponse)")
        }
    }

    func fetchLogs() async {
        await startJob("fetch logs") {
            try await bridge.fetchLogs(device: deviceName, kinds: [.steam, .proton])
        }
    }

    func screenshot() async {
        await startJob("screenshot") {
            try await bridge.screenshot(device: deviceName, mode: .fullComposition)
        }
    }

    func controllerConfig(_ gameid: String) async {
        await startJob("controller config") {
            try await bridge.controllerConfig(device: deviceName, gameid: gameid)
        }
    }

    func registerTitle(_ gameid: String) async {
        await run("register") {
            let result = try await bridge.registerTitle(device: deviceName, gameid: gameid)
            actionOutcome = .info(result.message ?? "title \(gameid) registered")
            await refresh()
        }
    }

    func beginDelete(_ title: Title) {
        titlePendingDeletion = title
        deleteConfirmationText = ""
    }

    func cancelDelete() {
        titlePendingDeletion = nil
        deleteConfirmationText = ""
    }

    func confirmDelete() async {
        guard let title = titlePendingDeletion, deleteConfirmed else { return }
        await run("delete") {
            let result = try await bridge.deleteTitle(device: deviceName, gameid: title.gameid)
            actionOutcome = .info(result.message ?? "deleted \(title.gameid)")
            if selectedGameID == title.gameid {
                selectedGameID = nil
                detail = nil
            }
            cancelDelete()
            await refresh()
        }
    }
}

struct TitlesView: View {
    @Environment(AppState.self) private var appState
    @State private var model: TitlesViewModel?

    let deviceName: String

    init(deviceName: String) {
        self.deviceName = deviceName
    }

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Loading titles…")
            }
        }
        .task {
            if model == nil {
                let vm = TitlesViewModel(bridge: appState.bridge, appState: appState, deviceName: deviceName)
                model = vm
                await vm.refresh()
            }
        }
        .sheet(item: deletionBinding) { row in
            if let model {
                DeleteTitleSheet(
                    row: row,
                    confirmationText: Binding(
                        get: { model.deleteConfirmationText },
                        set: { model.deleteConfirmationText = $0 }),
                    confirmed: model.deleteConfirmed,
                    onCancel: { model.cancelDelete() },
                    onDelete: { Task { await model.confirmDelete() } })
            }
        }
        .sheet(item: configBinding) { row in
            TitleConfigForm(deviceName: deviceName, gameid: row.id)
        }
    }

    private var deletionBinding: Binding<TitleRow?> {
        Binding(
            get: { model?.titlePendingDeletion.map(TitleRow.init) },
            set: { if $0 == nil { model?.cancelDelete() } })
    }

    private var configBinding: Binding<TitleRow?> {
        Binding(
            get: {
                model?.configuringGameID.flatMap { id in model?.titles.first { $0.gameid == id } }
                    .map(TitleRow.init)
            },
            set: { if $0 == nil { model?.configuringGameID = nil } })
    }

    @ViewBuilder
    private func content(_ model: TitlesViewModel) -> some View {
        VStack(spacing: 0) {
            if let outcome = model.actionOutcome {
                ErrorBanner(model: outcome) { model.actionOutcome = nil }
                    .padding([.horizontal, .top])
            }
            if let error = model.loadError {
                ErrorBanner(model: error) { Task { await model.refresh() } }
                    .padding()
            }
            TitlesTableView(appState: appState, model: model)
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button(action: { Task { await model.screenshot() } }) {
                    Label("Screenshot", systemImage: "camera")
                }
                .help("Capture the device screen (full composition)")
            }
            ToolbarItem(placement: .automatic) {
                Button(action: { Task { await model.fetchLogs() } }) {
                    Label("Fetch logs", systemImage: "doc.text")
                }
                .help("Pull steam and proton logs as artifacts")
            }
            ToolbarItem(placement: .automatic) {
                Button(action: { Task { await model.refresh() } }) {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(model.isLoading)
            }
        }
    }
}

/// Table + detail pane, split out so the selection binding stays simple.
private struct TitlesTableView: View {
    let appState: AppState
    @Bindable var model: TitlesViewModel

    private var selection: Binding<String?> {
        Binding(
            get: { model.selectedGameID },
            set: { gameid in Task { await model.select(gameid) } })
    }

    var body: some View {
        HSplitView {
            Table(model.rows, selection: selection) {
                TableColumn("gameid", value: \.id)
                TableColumn("size") { row in
                    Text(Format.bytes(row.title.sizeBytes))
                }
                .width(90)
                TableColumn("modified") { row in
                    Text(Format.date(row.title.modified))
                        .foregroundStyle(.secondary)
                }
                .width(150)
                TableColumn("steam") { row in
                    switch row.title.registeredWithSteam {
                    case .some(true):
                        Image(systemName: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                            .help("registered with Steam")
                    case .some(false):
                        Image(systemName: "circle")
                            .foregroundStyle(.secondary)
                            .help("not registered with Steam")
                    case .none:
                        Text("?")
                            .foregroundStyle(.secondary)
                            .help("registration not knowable from the device")
                    }
                }
                .width(50)
            }
            .contextMenu(forSelectionType: TitleRow.ID.self) { selection in
                if let gameid = selection.first {
                    Button("Launch") { Task { await model.launch(gameid) } }
                    Button("Configure…") { model.configuringGameID = gameid }
                    Button("Controller config") { Task { await model.controllerConfig(gameid) } }
                    Button("Register with Steam") { Task { await model.registerTitle(gameid) } }
                    Divider()
                    Button("Delete…", role: .destructive) {
                        if let title = model.titles.first(where: { $0.gameid == gameid }) {
                            model.beginDelete(title)
                        }
                    }
                }
            }
            .frame(minWidth: 420)

            detail
                .frame(minWidth: 300)
                .padding()
        }
    }

    @ViewBuilder
    private var detail: some View {
        if let detail = model.detail {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(detail.gameid).font(.title3.weight(.semibold))
                    if let remoteDir = detail.remoteDir {
                        Text(remoteDir)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    GroupBox("Launch configuration") {
                        VStack(alignment: .leading, spacing: 4) {
                            if let entrypoint = detail.config.entrypoint {
                                LabeledContent("entrypoint") {
                                    Text(([entrypoint] + detail.config.args).joined(separator: " "))
                                        .font(.system(.callout, design: .monospaced))
                                        .textSelection(.enabled)
                                }
                            } else {
                                Text("no entrypoint set")
                                    .foregroundStyle(.secondary)
                            }
                            LabeledContent("runtime") {
                                Text(detail.config.runtime?.rawValue ?? "none")
                            }
                            LabeledContent("env") {
                                Text("\(detail.config.env.count) var(s)")
                            }
                            LabeledContent("settings") {
                                Text("\(detail.config.settings.count) key(s)")
                            }
                        }
                        .font(.callout)
                    }
                    actionButtons(detail.gameid)
                }
            }
        } else {
            ContentUnavailableView(
                "Select a title",
                systemImage: "gamecontroller",
                description: Text("Launch, configure, or pull diagnostics for a devkit title."))
        }
    }

    @ViewBuilder
    private func actionButtons(_ gameid: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Button("Launch") { Task { await model.launch(gameid) } }
                    .buttonStyle(.borderedProminent)
                Button("Configure…") { model.configuringGameID = gameid }
                Button("Controller config") { Task { await model.controllerConfig(gameid) } }
            }
            HStack(spacing: 8) {
                Button("Register with Steam") { Task { await model.registerTitle(gameid) } }
                Spacer()
                Button("Delete…", role: .destructive) {
                    if let title = model.titles.first(where: { $0.gameid == gameid }) {
                        model.beginDelete(title)
                    }
                }
            }
        }
    }
}

private struct DeleteTitleSheet: View {
    let row: TitleRow
    @Binding var confirmationText: String
    let confirmed: Bool
    let onCancel: () -> Void
    let onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Delete \(row.id)?", systemImage: "trash")
                .font(.headline)
            Text(
                "This removes the title and its uploaded files from the device. "
                    + "Type the gameid to confirm."
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            TextField(row.id, text: $confirmationText)
                .textFieldStyle(.roundedBorder)
            HStack {
                Spacer()
                Button("Cancel", action: onCancel)
                    .keyboardShortcut(.cancelAction)
                Button("Delete", role: .destructive, action: onDelete)
                    .disabled(!confirmed)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 380)
    }
}

struct TitlesView_Previews: PreviewProvider {
    static var previews: some View {
        TitlesView(deviceName: "steamdeck")
            .environment(AppState.preview())
    }
}
