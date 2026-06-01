import Foundation

enum SharingMode: String, CaseIterable, Identifiable {
    case localOnly
    case tailscaleServe

    var id: String { rawValue }

    var label: String {
        switch self {
        case .localOnly:
            return "Local Only"
        case .tailscaleServe:
            return "Tailnet via Tailscale Serve"
        }
    }
}
