import AppKit
import SwiftUI

// DeploySheet.swift — the deploy pipeline UI: source dir picker, live gameid
// validation (regex + reserved ids), filters, mutually-exclusive
// checksum/update toggles, optional title config, preview (dry-run), and
// deploy → job registration + navigation to the job.

@MainActor
@Observable
final class DeployViewModel {
    let bridge: any DevkitBridge
    let appState: AppState

    var device: String
    var gameid = ""
    var sourceDir = ""
    var filterMode: FilterMode = .exclude
    var filterPatterns: [String] = []
    var newPattern = ""
    var clean = false
    var checksum = false
    var update = false
    var compress = true
    var register = true
    var launch = false
    var includesTitleConfig = false
    let configDraft = TitleConfigDraft()

    var preview: DeployPreview?
    var isPreviewing = false
    var isDeploying = false
    var formError: BannerModel?
    /// Set once deploy starts; the sheet dismisses itself.
    var startedJobID: String?

    init(bridge: any DevkitBridge, appState: AppState, device: String?) {
        self.bridge = bridge
        self.appState = appState
        self.device = device ?? ""
    }

    /// nil while the field is empty (no nagging before typing).
    var gameIDValidationError: String? {
        guard !gameid.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
        return Validation.gameIDError(gameid)
    }

    var canDeploy: Bool {
        Validation.gameIDError(gameid) == nil
            && !sourceDir.trimmingCharacters(in: .whitespaces).isEmpty
            && !device.isEmpty
            && !isDeploying && !isPreviewing
    }

    /// checksum and update are mutually exclusive (server also validates).
    func setChecksum(_ on: Bool) {
        checksum = on
        if on { update = false }
    }

    func setUpdate(_ on: Bool) {
        update = on
        if on { checksum = false }
    }

    func addPattern() {
        let pattern = newPattern.trimmingCharacters(in: .whitespaces)
        guard !pattern.isEmpty else { return }
        filterPatterns.append(pattern)
        newPattern = ""
    }

    func removePattern(_ pattern: String) {
        filterPatterns.removeAll { $0 == pattern }
    }

    func makeSpec() -> DeploySpec? {
        guard canDeploy else { return nil }
        return DeploySpec(
            device: device,
            gameid: gameid.trimmingCharacters(in: .whitespaces),
            sourceDir: sourceDir,
            filters: FilterSpec(mode: filterMode, patterns: filterPatterns),
            clean: clean,
            checksum: checksum,
            update: update,
            compress: compress,
            register: register,
            launch: launch,
            config: includesTitleConfig ? configDraft.makeConfig() : nil)
    }

    func pickSourceDir() async {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose build directory"
        let response = await panel.begin()
        if response == .OK, let url = panel.url {
            sourceDir = url.path
        }
    }

    func runPreview() async {
        guard !isPreviewing else { return }
        guard Validation.gameIDError(gameid) == nil, !sourceDir.isEmpty, !device.isEmpty else {
            formError = .info("pick a device, a valid gameid, and a source directory first")
            return
        }
        isPreviewing = true
        formError = nil
        defer { isPreviewing = false }
        do {
            preview = try await bridge.previewDeploy(
                device: device,
                gameid: gameid.trimmingCharacters(in: .whitespaces),
                sourceDir: sourceDir,
                filters: FilterSpec(mode: filterMode, patterns: filterPatterns),
                clean: clean)
        } catch {
            if let model = ErrorPresentation.model(for: error) {
                formError = model
            }
        }
    }

    func deploy() async {
        guard let spec = makeSpec() else {
            formError = .info("pick a device, a valid gameid, and a source directory first")
            return
        }
        isDeploying = true
        formError = nil
        defer { isDeploying = false }
        do {
            let start = try await bridge.deploy(spec)
            appState.registerJob(start)
            startedJobID = start.jobId
            appState.showJob(start.jobId)
        } catch {
            if let model = ErrorPresentation.model(for: error) {
                formError = model
            }
        }
    }
}

struct DeploySheet: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var model: DeployViewModel?

    let initialDevice: String?

    init(device: String?) {
        initialDevice = device
    }

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Preparing deploy…")
            }
        }
        .frame(minWidth: 640, minHeight: 560)
        .task {
            if model == nil {
                model = DeployViewModel(
                    bridge: appState.bridge, appState: appState, device: initialDevice)
            }
        }
        .onChange(of: model?.startedJobID) { _, jobID in
            if jobID != nil { dismiss() }
        }
    }

    @ViewBuilder
    private func content(_ model: DeployViewModel) -> some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            HStack {
                Text("Deploy a build")
                    .font(.title3.weight(.semibold))
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .padding([.horizontal, .top])

            if let error = model.formError {
                ErrorBanner(model: error) { model.formError = nil }
                    .padding([.horizontal, .top])
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    targetSection(model)
                    sourceSection(model)
                    filtersSection(model)
                    optionsSection(model)
                    configSection(model)
                    if let preview = model.preview {
                        previewSection(preview)
                    }
                }
                .padding()
            }

            HStack {
                Button(action: { Task { await model.runPreview() } }) {
                    if model.isPreviewing {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("Preview")
                    }
                }
                .disabled(model.isPreviewing || model.isDeploying)
                .help("Dry-run: show what would change, touch nothing")
                Spacer()
                Button(action: { Task { await model.deploy() } }) {
                    if model.isDeploying {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("Deploy")
                    }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!model.canDeploy)
            }
            .padding()
        }
    }

    @ViewBuilder
    private func targetSection(_ model: DeployViewModel) -> some View {
        @Bindable var model = model
        GroupBox("Target") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 12) {
                    Picker("Device", selection: $model.device) {
                        ForEach(appState.devices, id: \.name) { device in
                            Text(device.name).tag(device.name)
                        }
                    }
                    .frame(maxWidth: 240)
                    TextField("gameid", text: $model.gameid)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.callout, design: .monospaced))
                        .frame(maxWidth: 260)
                }
                if let error = model.gameIDValidationError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                } else if !model.gameid.isEmpty {
                    Text("letters, digits, “_” and “.”; reserved ids (steam, steamdeckard, steamvr, steamvrdeckard) are rejected")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    @ViewBuilder
    private func sourceSection(_ model: DeployViewModel) -> some View {
        @Bindable var model = model
        GroupBox("Source") {
            HStack(spacing: 8) {
                Text(model.sourceDir.isEmpty ? "no directory chosen" : model.sourceDir)
                    .font(.system(.callout, design: .monospaced))
                    .foregroundStyle(model.sourceDir.isEmpty ? .secondary : .primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                Spacer()
                Button("Choose…") { Task { await model.pickSourceDir() } }
            }
        }
    }

    @ViewBuilder
    private func filtersSection(_ model: DeployViewModel) -> some View {
        @Bindable var model = model
        GroupBox("Filters") {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Mode", selection: $model.filterMode) {
                    Text("exclude").tag(FilterMode.exclude)
                    Text("include").tag(FilterMode.include)
                    Text("raw rsync filters").tag(FilterMode.raw)
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 380)
                HStack(spacing: 8) {
                    TextField("filter pattern, e.g. *.log", text: $model.newPattern)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.callout, design: .monospaced))
                        .onSubmit { model.addPattern() }
                    Button("Add") { model.addPattern() }
                        .disabled(model.newPattern.trimmingCharacters(in: .whitespaces).isEmpty)
                }
                if !model.filterPatterns.isEmpty {
                    ForEach(model.filterPatterns, id: \.self) { pattern in
                        HStack {
                            Text(pattern)
                                .font(.system(.callout, design: .monospaced))
                            Spacer()
                            Button(action: { model.removePattern(pattern) }) {
                                Image(systemName: "minus.circle")
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func optionsSection(_ model: DeployViewModel) -> some View {
        @Bindable var model = model
        GroupBox("Options") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 16) {
                    Toggle("Compress (-z)", isOn: $model.compress)
                    Toggle("Register with Steam", isOn: $model.register)
                    Toggle("Launch after deploy", isOn: $model.launch)
                }
                HStack(spacing: 16) {
                    Toggle("Clean (delete remote-only files)", isOn: $model.clean)
                    Toggle("Checksum compare", isOn: Binding(
                        get: { model.checksum }, set: { model.setChecksum($0) }))
                    Toggle("Update (skip newer remote)", isOn: Binding(
                        get: { model.update }, set: { model.setUpdate($0) }))
                }
                Text("checksum and update are mutually exclusive — selecting one clears the other.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder
    private func configSection(_ model: DeployViewModel) -> some View {
        @Bindable var model = model
        DisclosureGroup(isExpanded: $model.includesTitleConfig) {
            TitleConfigFields(draft: model.configDraft)
                .padding(.top, 6)
        } label: {
            Text("Launch configuration (optional)")
                .font(.callout.weight(.medium))
        }
        .padding(.horizontal, 4)
    }

    @ViewBuilder
    private func previewSection(_ preview: DeployPreview) -> some View {
        GroupBox("Preview") {
            VStack(alignment: .leading, spacing: 8) {
                Text(
                    "+\(preview.filesAdded) added, ~\(preview.filesChanged) changed, "
                        + "-\(preview.filesDeleted) deleted, "
                        + "\(Format.bytes(preview.bytesToTransfer)) to transfer"
                )
                .font(.callout.weight(.medium))
                if !preview.sampleChanges.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(preview.sampleChanges.prefix(8), id: \.self) { change in
                            Text(change)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        if preview.sampleChanges.count > 8 {
                            Text("… \(preview.sampleChanges.count - 8) more")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                ForEach(preview.warnings, id: \.self) { warning in
                    Label(warning, systemImage: "exclamationmark.triangle.fill")
                        .font(.callout)
                        .foregroundStyle(.yellow)
                }
            }
        }
    }
}

struct DeploySheet_Previews: PreviewProvider {
    static var previews: some View {
        DeploySheet(device: "steamdeck")
            .environment(AppState.preview(scenario: .deployWithProgress))
    }
}
