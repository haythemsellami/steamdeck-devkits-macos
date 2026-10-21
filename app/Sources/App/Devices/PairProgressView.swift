import SwiftUI

// PairProgressView.swift — the pairing wait state. Pairing is long: the user
// approves on the Deck while the server waits (120 s). Shows guidance, a
// spinner, and Cancel (abandons the local wait only). Success/failure render
// through the same view via the model's pairPhase.

struct PairProgressView: View {
    let model: DiscoverViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch model.pairPhase {
            case .idle:
                EmptyView()
            case .inProgress:
                HStack(spacing: 10) {
                    ProgressView()
                        .controlSize(.regular)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Pairing with \(model.pairingTarget)…")
                            .font(.headline)
                        Text(
                            "Approve the pairing request on your Steam Deck. "
                                + "This Mac waits up to two minutes; the on-device prompt "
                                + "expires on its own if you cancel here."
                        )
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Cancel") {
                        model.cancelPair()
                    }
                }
            case .succeeded(let message):
                HStack(spacing: 10) {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .font(.title2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Paired with \(model.pairingTarget)")
                            .font(.headline)
                        Text(message)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Pair another…") {
                        model.resetPair()
                    }
                }
            case .failed(let banner):
                VStack(alignment: .leading, spacing: 8) {
                    ErrorBanner(model: banner)
                    HStack {
                        Spacer()
                        Button("Try again") {
                            model.resetPair()
                        }
                    }
                }
            }
        }
        .padding(12)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct PairProgressView_Previews: PreviewProvider {
    static var previews: some View {
        let appState = AppState.preview(scenario: .populated)
        let model = DiscoverViewModel(bridge: appState.bridge, appState: appState)
        model.pairingTarget = "steamdeck"
        return PairProgressView(model: model)
            .padding()
            .frame(width: 520)
    }
}
