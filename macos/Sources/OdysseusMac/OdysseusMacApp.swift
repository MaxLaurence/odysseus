import AppKit
import SwiftUI

@main
struct OdysseusMacApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var backend = BackendController.shared

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

/// Intercepts Cmd-Q / Quit so running agents aren't silently orphaned.
///
/// When agents are running, the user picks: keep them running headless (the
/// historical behavior — reopening Odysseus reconnects), stop everything and shut
/// the backend down, or cancel. When nothing is running there's no prompt: the app
/// quits and the (idle) backend is left in place exactly as before.
final class AppDelegate: NSObject, NSApplicationDelegate {
    private enum QuitChoice {
        case leaveRunning
        case quitEverything
        case cancel
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // The active-task count requires an async backend round trip, so defer the
        // decision and report it back via reply(toApplicationShouldTerminate:).
        Task { @MainActor in
            let backend = BackendController.shared
            let activeCount = await backend.activeTaskCount()
            guard activeCount > 0 else {
                NSApp.reply(toApplicationShouldTerminate: true)
                return
            }
            switch self.presentQuitPrompt(activeCount: activeCount) {
            case .leaveRunning:
                // Leave the backend + agents running headless; just close the UI.
                NSApp.reply(toApplicationShouldTerminate: true)
            case .quitEverything:
                await backend.stopAllTasks()
                backend.stopBackend()
                NSApp.reply(toApplicationShouldTerminate: true)
            case .cancel:
                NSApp.reply(toApplicationShouldTerminate: false)
            }
        }
        return .terminateLater
    }

    @MainActor
    private func presentQuitPrompt(activeCount: Int) -> QuitChoice {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = activeCount == 1
            ? "1 agent is still running"
            : "\(activeCount) agents are still running"
        alert.informativeText = """
            Leave them running and Odysseus keeps working in the background — \
            reopening the app reconnects to them. Quit everything to stop all \
            running agents and shut the backend down.
            """
        // Order sets the key bindings: first = default (Return), a button titled
        // "Cancel" auto-maps to Escape.
        alert.addButton(withTitle: "Leave Running")    // .alertFirstButtonReturn
        alert.addButton(withTitle: "Quit Everything")  // .alertSecondButtonReturn
        alert.addButton(withTitle: "Cancel")           // .alertThirdButtonReturn
        NSApp.activate(ignoringOtherApps: true)
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            return .leaveRunning
        case .alertSecondButtonReturn:
            return .quitEverything
        default:
            return .cancel
        }
    }
}
