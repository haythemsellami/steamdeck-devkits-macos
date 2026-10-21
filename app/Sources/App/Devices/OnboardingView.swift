import SwiftUI

// OnboardingView.swift — the empty-registry state of the Devices section:
// pair a Deck, add by IP, and a compact host-doctor summary with a Fix-it
// link when something needs attention.

struct OnboardingView: View {
    @Environment(AppState.self) private var appState

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            Image(systemName: "gamecontroller")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            VStack(spacing: 6) {
                Text("Pair your Steam Deck")
                    .font(.title.weight(.semibold))
                Text(
                    "No devices registered yet. Discover a SteamOS devkit on your "
                        + "local network, or connect directly by IP address."
                )
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            }
            HStack(spacing: 12) {
                Button(action: { appState.showsDiscoverSheet = true }) {
                    Label("Discover devices…", systemImage: "bonjour")
                }
                .controlSize(.large)
                .buttonStyle(.borderedProminent)
                Button(action: { appState.showsDiscoverSheet = true }) {
                    Label("Add by IP…", systemImage: "network")
                }
                .controlSize(.large)
            }
            doctorSummary
            Spacer()
        }
        .frame(maxWidth: .infinity)
        .navigationTitle("Devices")
        .task {
            if appState.doctorReport == nil {
                await appState.refreshDoctor()
            }
        }
    }

    @ViewBuilder
    private var doctorSummary: some View {
        if let report = appState.doctorReport {
            let blocked = appState.hostLocalNetwork == .blocked
            HStack(spacing: 8) {
                Image(systemName: report.ok && !blocked ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                    .foregroundStyle(report.ok && !blocked ? .green : .orange)
                if blocked {
                    Text("Local Network access is blocked — discovery and device contact will fail.")
                        .font(.callout)
                } else {
                    Text(
                        report.ok
                            ? "Host checks passed (\(report.platform), \(report.arch))"
                            : "Some host checks need attention."
                    )
                    .font(.callout)
                }
                if !report.ok || blocked {
                    Button("Fix it in Doctor") {
                        appState.showDoctor()
                    }
                    .font(.callout)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
        } else {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Checking this Mac's setup…")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

struct OnboardingView_Previews: PreviewProvider {
    static var previews: some View {
        OnboardingView()
            .environment(AppState.preview(scenario: .empty))
    }
}
