import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var backend: BackendController

    var body: some View {
        Form {
            Picker("Sharing", selection: $backend.sharingMode) {
                ForEach(SharingMode.allCases) { mode in
                    Text(mode.label).tag(mode)
                }
            }
            Text("Local Only keeps Odysseus bound to 127.0.0.1. Tailnet sharing starts `tailscale serve` against the local backend port.")
                .font(.caption)
                .foregroundStyle(.secondary)

            TextField("Model discovery ports", text: $backend.llmPorts)
            Text("Comma-separated ports and ranges, for example: 1337,8000-8020,11434")
                .font(.caption)
                .foregroundStyle(.secondary)

            Button("Show Backend Log") {
                backend.openLogs()
            }
            .disabled(backend.logURL == nil)
        }
        .padding(20)
        .frame(width: 520)
    }
}
