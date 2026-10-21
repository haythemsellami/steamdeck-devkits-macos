import SwiftUI

// TitleConfigForm.swift — edit a title's launch sidecars. Form state lives in
// TitleConfigDraft (reused by the deploy sheet's optional config section);
// TitleConfigFormModel adds load/save against the bridge.
//
// Merge semantics (H1, spec appendix A): a nil entrypoint and EMPTY
// collections mean "leave unchanged" — the form says so next to each field.

/// Pure form state for a TitleConfig. No bridge; usable from any sheet.
@MainActor
@Observable
final class TitleConfigDraft {
    var entrypoint = ""
    /// One argument per line.
    var argsText = ""
    var envRows: [KeyValueRow] = []
    /// nil = leave unchanged (server merge semantics).
    var runtime: CompatToolAlias?
    var settingsRows: [KeyValueRow] = []

    init() {}

    init(config: TitleConfig) {
        entrypoint = config.entrypoint ?? ""
        argsText = config.args.joined(separator: "\n")
        envRows = KeyValueRow.rows(from: config.env)
        runtime = config.runtime
        settingsRows = KeyValueRow.rows(from: config.settings)
    }

    var parsedArgs: [String] {
        argsText
            .components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    /// Builds the DTO with leave-unchanged semantics: empty entrypoint → nil,
    /// empty collections → empty (server leaves those untouched).
    func makeConfig() -> TitleConfig {
        let trimmed = entrypoint.trimmingCharacters(in: .whitespaces)
        return TitleConfig(
            entrypoint: trimmed.isEmpty ? nil : trimmed,
            args: parsedArgs,
            env: KeyValueRow.dict(from: envRows),
            runtime: runtime,
            settings: KeyValueRow.dict(from: settingsRows))
    }
}

@MainActor
@Observable
final class TitleConfigFormModel {
    let bridge: any DevkitBridge
    let appState: AppState
    let deviceName: String
    let gameid: String

    let draft = TitleConfigDraft()
    var isLoading = false
    var isSaving = false
    var didLoad = false
    var loadError: BannerModel?
    var saveOutcome: BannerModel?

    init(bridge: any DevkitBridge, appState: AppState, deviceName: String, gameid: String) {
        self.bridge = bridge
        self.appState = appState
        self.deviceName = deviceName
        self.gameid = gameid
    }

    /// Prefills the form with the stored on-device configuration.
    func load() async {
        guard !didLoad, !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let detail = try await bridge.inspectTitle(device: deviceName, gameid: gameid)
            draft.entrypoint = detail.config.entrypoint ?? ""
            draft.argsText = detail.config.args.joined(separator: "\n")
            draft.envRows = KeyValueRow.rows(from: detail.config.env)
            draft.runtime = detail.config.runtime
            draft.settingsRows = KeyValueRow.rows(from: detail.config.settings)
            didLoad = true
            loadError = nil
        } catch {
            loadError = ErrorPresentation.model(for: error)
        }
    }

    func save() async {
        guard !isSaving else { return }
        isSaving = true
        defer { isSaving = false }
        do {
            let result = try await bridge.configureTitle(
                device: deviceName, gameid: gameid, config: draft.makeConfig())
            saveOutcome = .info(result.message ?? "title \(gameid) configured")
        } catch {
            saveOutcome = ErrorPresentation.model(for: error)
        }
    }
}

/// Shared fields for editing a TitleConfigDraft.
struct TitleConfigFields: View {
    @Bindable var draft: TitleConfigDraft

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Entrypoint")
                    .font(.callout.weight(.medium))
                TextField("start command, relative to the game directory", text: $draft.entrypoint)
                    .textFieldStyle(.roundedBorder)
                    .font(.system(.callout, design: .monospaced))
                Text("Empty keeps the current entrypoint.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Arguments — one per line")
                    .font(.callout.weight(.medium))
                TextEditor(text: $draft.argsText)
                    .font(.system(.callout, design: .monospaced))
                    .frame(minHeight: 56, maxHeight: 90)
                    .border(Color.secondary.opacity(0.3))
                Text("Empty keeps the current arguments.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Environment")
                    .font(.callout.weight(.medium))
                KeyValueEditor(rows: $draft.envRows, keyPlaceholder: "KEY", valuePlaceholder: "value")
                Text("No rows keeps the current environment.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Runtime (compat_tool)")
                    .font(.callout.weight(.medium))
                Picker("Runtime", selection: $draft.runtime) {
                    Text("leave unchanged").tag(CompatToolAlias?.none)
                    Divider()
                    ForEach(CompatToolAlias.allCases, id: \.self) { alias in
                        Text(alias.rawValue).tag(CompatToolAlias?.some(alias))
                    }
                }
                .labelsHidden()
                .frame(maxWidth: 280)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Settings")
                    .font(.callout.weight(.medium))
                KeyValueEditor(rows: $draft.settingsRows, keyPlaceholder: "steam_play", valuePlaceholder: "value")
                Text("No rows keeps the current settings.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct TitleConfigForm: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var model: TitleConfigFormModel?

    let deviceName: String
    let gameid: String

    init(deviceName: String, gameid: String) {
        self.deviceName = deviceName
        self.gameid = gameid
    }

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Loading \(gameid)…")
            }
        }
        .frame(minWidth: 520, minHeight: 480)
        .task {
            if model == nil {
                let vm = TitleConfigFormModel(
                    bridge: appState.bridge, appState: appState,
                    deviceName: deviceName, gameid: gameid)
                model = vm
                await vm.load()
            }
        }
    }

    @ViewBuilder
    private func content(_ model: TitleConfigFormModel) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text("Configure \(gameid)")
                    .font(.title3.weight(.semibold))
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(action: { Task { await model.save() } }) {
                    if model.isSaving {
                        ProgressView().controlSize(.small)
                    } else {
                        Text("Save")
                    }
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(model.isSaving || model.isLoading)
            }
            .padding()

            if let outcome = model.saveOutcome {
                ErrorBanner(model: outcome) { model.saveOutcome = nil }
                    .padding([.horizontal, .bottom])
            }
            if let error = model.loadError {
                ErrorBanner(model: error)
                    .padding([.horizontal, .bottom])
            }

            ScrollView {
                TitleConfigFields(draft: model.draft)
                    .padding([.horizontal, .bottom])
                    .disabled(model.isLoading)
            }
        }
    }
}

struct TitleConfigForm_Previews: PreviewProvider {
    static var previews: some View {
        TitleConfigForm(deviceName: "steamdeck", gameid: "zz_devkit_test")
            .environment(AppState.preview())
    }
}
