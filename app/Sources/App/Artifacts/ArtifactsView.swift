import AppKit
import SwiftUI

// ArtifactsView.swift — capture outputs: table (kind, device, bytes,
// created), preview pane (text excerpts monospaced, images via
// artifactData → NSImage), metadata grid (sha256, remote source, job link),
// Reveal in Finder / Open for files that exist on disk.

@MainActor
@Observable
final class ArtifactsViewModel {
    enum PreviewState: Equatable {
        case idle
        case loading
        case text(String)
        case image(Data)
        /// Binary or unsupported kind — preview not offered.
        case noPreview
        case failed(BannerModel)
    }

    enum FileState: Equatable {
        case file
        case directory
        case missing
    }

    let bridge: any DevkitBridge
    let appState: AppState

    var selectedArtifactID: String?
    var preview: PreviewState = .idle
    var error: BannerModel?

    init(bridge: any DevkitBridge, appState: AppState) {
        self.bridge = bridge
        self.appState = appState
    }

    var entries: [ArtifactEntry] {
        appState.artifacts
    }

    func refresh() async {
        await appState.refreshArtifacts()
    }

    func fileState(of entry: ArtifactEntry) -> FileState {
        var isDirectory: ObjCBool = false
        let exists = FileManager.default.fileExists(
            atPath: entry.artifact.path, isDirectory: &isDirectory)
        if !exists { return .missing }
        return isDirectory.boolValue ? .directory : .file
    }

    func select(_ artifactID: String?) async {
        selectedArtifactID = artifactID
        guard let artifactID,
              let entry = entries.first(where: { $0.id == artifactID })
        else {
            preview = .idle
            return
        }
        let artifact = entry.artifact
        let isText = artifact.mediaType?.hasPrefix("text/") == true
            || artifact.kind == .logs || artifact.kind == .protonLog
            || artifact.kind == .controllerConfig || artifact.kind == .frametimes
        let isImage = artifact.kind == .screenshot
            || artifact.mediaType?.hasPrefix("image/") == true
        preview = .loading
        do {
            if isText {
                let read = try await bridge.readArtifact(artifactId: artifactID, tailLines: 200)
                preview = read.excerpt.map { .text($0) } ?? .noPreview
            } else if isImage {
                let payload = try await bridge.artifactData(artifactId: artifactID)
                preview = .image(payload.data)
            } else {
                preview = .noPreview
            }
        } catch {
            if let model = ErrorPresentation.model(for: error) {
                preview = .failed(model)
            }
        }
    }

    /// Image bytes → NSImage; nil when the data isn't a decodable image.
    func makeImage(_ data: Data) -> NSImage? {
        NSImage(data: data)
    }

    func reveal(_ entry: ArtifactEntry) {
        NSWorkspace.shared.selectFile(
            entry.artifact.path,
            inFileViewerRootedAtPath: (entry.artifact.path as NSString).deletingLastPathComponent)
    }

    func open(_ entry: ArtifactEntry) {
        NSWorkspace.shared.open(URL(fileURLWithPath: entry.artifact.path))
    }
}

struct ArtifactsView: View {
    @Environment(AppState.self) private var appState
    @State private var model: ArtifactsViewModel?

    var body: some View {
        Group {
            if let model {
                content(model)
            } else {
                ProgressView("Loading artifacts…")
            }
        }
        .navigationTitle("Artifacts")
        .task {
            if model == nil {
                let vm = ArtifactsViewModel(bridge: appState.bridge, appState: appState)
                model = vm
                await vm.refresh()
                if let highlight = appState.highlightedArtifactID {
                    appState.highlightedArtifactID = nil
                    await vm.select(highlight)
                }
            }
        }
    }

    @ViewBuilder
    private func content(_ model: ArtifactsViewModel) -> some View {
        VStack(spacing: 0) {
            if let error = model.error {
                ErrorBanner(model: error) { model.error = nil }
                    .padding([.horizontal, .top])
            }
            if model.entries.isEmpty {
                ContentUnavailableView(
                    "No artifacts yet",
                    systemImage: "tray",
                    description: Text("Logs, screenshots, and captures from jobs land here."))
            } else {
                HSplitView {
                    table(model)
                        .frame(minWidth: 430)
                    detail(model)
                        .frame(minWidth: 340)
                }
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
    private func table(_ model: ArtifactsViewModel) -> some View {
        Table(
            model.entries,
            selection: Binding(
                get: { model.selectedArtifactID },
                set: { artifactID in Task { await model.select(artifactID) } }))
        {
            TableColumn("") { entry in
                ArtifactKindIcon(kind: entry.artifact.kind)
            }
            .width(24)
            TableColumn("artifact") { entry in
                Text(entry.artifact.kind.rawValue.replacingOccurrences(of: "_", with: " "))
            }
            TableColumn("device") { entry in
                Text(entry.artifact.device)
            }
            .width(100)
            TableColumn("bytes") { entry in
                Text(Format.bytes(entry.artifact.bytes))
            }
            .width(90)
            TableColumn("created") { entry in
                Text(Format.date(entry.artifact.createdAt))
                    .foregroundStyle(.secondary)
            }
            .width(150)
        }
    }

    @ViewBuilder
    private func detail(_ model: ArtifactsViewModel) -> some View {
        if let artifactID = model.selectedArtifactID,
           let entry = model.entries.first(where: { $0.id == artifactID })
        {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    previewPane(model)
                    metadataGrid(model, entry)
                    actionRow(model, entry)
                }
                .padding()
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            ContentUnavailableView(
                "Select an artifact",
                systemImage: "doc.magnifyingglass",
                description: Text("Preview logs and screenshots, or reveal captures in Finder."))
        }
    }

    @ViewBuilder
    private func previewPane(_ model: ArtifactsViewModel) -> some View {
        switch model.preview {
        case .idle:
            EmptyView()
        case .loading:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Loading preview…").foregroundStyle(.secondary)
            }
        case .text(let excerpt):
            ScrollView {
                Text(excerpt)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
            }
            .frame(minHeight: 160, maxHeight: 280)
            .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
        case .image(let data):
            if let nsImage = model.makeImage(data) {
                Image(nsImage: nsImage)
                    .resizable()
                    .scaledToFit()
                    .frame(maxWidth: .infinity, maxHeight: 300)
                    .background(Color.black.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
            } else {
                Label("image data could not be decoded", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
        case .noPreview:
            Label("no inline preview for this kind — use Reveal or Open", systemImage: "doc.zipper")
                .font(.callout)
                .foregroundStyle(.secondary)
        case .failed(let banner):
            ErrorBanner(model: banner)
        }
    }

    @ViewBuilder
    private func metadataGrid(_ model: ArtifactsViewModel, _ entry: ArtifactEntry) -> some View {
        let artifact = entry.artifact
        GroupBox("Metadata") {
            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                GridRow {
                    Text("sha256").foregroundStyle(.secondary)
                    Text(String(artifact.sha256.prefix(16)) + "…")
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .help(artifact.sha256)
                }
                GridRow {
                    Text("remote source").foregroundStyle(.secondary)
                    Text(artifact.remoteSource)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
                GridRow {
                    Text("resource").foregroundStyle(.secondary)
                    Text(entry.resourceUri)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
                GridRow {
                    Text("job").foregroundStyle(.secondary)
                    Button(artifact.jobId) {
                        appState.showJob(artifact.jobId)
                    }
                    .buttonStyle(.link)
                }
            }
            .font(.callout)
        }
    }

    @ViewBuilder
    private func actionRow(_ model: ArtifactsViewModel, _ entry: ArtifactEntry) -> some View {
        let fileState = model.fileState(of: entry)
        HStack(spacing: 8) {
            switch fileState {
            case .file:
                Button("Reveal in Finder") { model.reveal(entry) }
                Button("Open") { model.open(entry) }
            case .directory:
                Text("folder artifact")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Reveal in Finder") { model.reveal(entry) }
            case .missing:
                Text("not on this Mac (\(entry.artifact.path))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
    }
}

struct ArtifactsView_Previews: PreviewProvider {
    static var previews: some View {
        ArtifactsView()
            .environment(AppState.preview())
    }
}
