import SwiftUI

@main
struct OdysseusMacApp: App {
    @StateObject private var backend = BackendController()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(backend)
                .frame(minWidth: 1024, minHeight: 720)
                .onAppear {
                    backend.startIfNeeded()
                }
                .onDisappear {
                    backend.stopSharing()
                }
        }
        .commands {
            CommandMenu("Odysseus") {
                Button("Restart Backend") {
                    Task { await backend.restart() }
                }
                .keyboardShortcut("r", modifiers: [.command, .shift])

                Button("Open in Browser") {
                    backend.openInBrowser()
                }
                .disabled(backend.url == nil)

                Divider()

                Picker("Sharing", selection: $backend.sharingMode) {
                    ForEach(SharingMode.allCases) { mode in
                        Text(mode.label).tag(mode)
                    }
                }
            }
        }

        Settings {
            SettingsView()
                .environmentObject(backend)
        }
    }
}
