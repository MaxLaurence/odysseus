import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var backend: BackendController

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let url = backend.url {
                WebView(url: url)
            } else {
                VStack(spacing: 14) {
                    ProgressView()
                    Text(backend.statusText)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("Show Backend Log") {
                        backend.openLogs()
                    }
                    .disabled(backend.logURL == nil)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Odysseus")
                    .font(.headline)
                Text(backend.sharingStatus)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer()

            Picker("Sharing", selection: $backend.sharingMode) {
                ForEach(SharingMode.allCases) { mode in
                    Text(mode.label).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .frame(width: 310)

            Button("Logs") {
                backend.openLogs()
            }
            .disabled(backend.logURL == nil)

            Button("Browser") {
                backend.openInBrowser()
            }
            .disabled(backend.url == nil)

            Button("Restart") {
                Task { await backend.restart() }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}
