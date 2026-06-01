import AppKit
import Darwin
import Foundation

@MainActor
final class BackendController: ObservableObject {
    @Published private(set) var statusText = "Starting Odysseus..."
    @Published private(set) var url: URL?
    @Published private(set) var logURL: URL?
    @Published private(set) var sharingStatus = "Sharing is off."
    @Published var llmPorts: String {
        didSet {
            UserDefaults.standard.set(llmPorts, forKey: Self.llmPortsKey)
        }
    }
    @Published var sharingMode: SharingMode {
        didSet {
            UserDefaults.standard.set(sharingMode.rawValue, forKey: Self.sharingModeKey)
            syncSharingProcess()
        }
    }

    private static let sharingModeKey = "OdysseusSharingMode"
    private static let llmPortsKey = "OdysseusLLMPorts"

    private var backendProcess: Process?
    private var tailscaleProcess: Process?
    private var logHandle: FileHandle?
    private var currentPort: Int?
    private var isStarting = false
    private var startupTask: Task<Void, Never>?

    init() {
        let savedMode = UserDefaults.standard.string(forKey: Self.sharingModeKey)
        sharingMode = SharingMode(rawValue: savedMode ?? "") ?? .localOnly
        llmPorts = UserDefaults.standard.string(forKey: Self.llmPortsKey) ?? "1337,8000-8020,11434"
    }

    func startIfNeeded() {
        if startupTask != nil || backendProcess?.isRunning == true {
            return
        }

        startupTask = Task { [weak self] in
            guard let self else { return }
            await self.start()
            self.startupTask = nil
        }
    }

    func start() async {
        if isStarting || backendProcess?.isRunning == true {
            return
        }
        isStarting = true
        defer { isStarting = false }

        do {
            let port = try Self.findFreePort()
            currentPort = port
            let appSupport = try Self.applicationSupportDirectory()
            let dataDir = appSupport.appendingPathComponent("data", isDirectory: true)
            let logsDir = appSupport.appendingPathComponent("logs", isDirectory: true)
            try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: logsDir, withIntermediateDirectories: true)

            let log = logsDir.appendingPathComponent("backend.log")
            FileManager.default.createFile(atPath: log.path, contents: nil)
            logURL = log
            let handle = try FileHandle(forWritingTo: log)
            try handle.truncate(atOffset: 0)
            logHandle = handle

            let launch = try Self.resolveBackendLaunch()
            let process = Process()
            process.executableURL = launch.executableURL
            process.arguments = launch.arguments(port: port)
            process.currentDirectoryURL = launch.workingDirectory
            process.environment = launch.environment(
                port: port,
                appSupport: appSupport,
                dataDir: dataDir,
                llmPorts: llmPorts
            )
            process.standardOutput = logHandle
            process.standardError = logHandle
            try process.run()
            backendProcess = process
            writeWrapperLog("launched backend pid=\(process.processIdentifier) port=\(port)")

            let localURL = URL(string: "http://127.0.0.1:\(port)")!
            statusText = "Waiting for Odysseus at \(localURL.absoluteString)..."
            try await waitForHealth(port: port)
            url = localURL
            statusText = "Odysseus is running."
            writeWrapperLog("backend ready url=\(localURL.absoluteString)")
            syncSharingProcess()
        } catch {
            writeWrapperLog("startup failed: \(error.localizedDescription)")
            if url == nil {
                stopBackend()
            }
            statusText = "Failed to start Odysseus: \(error.localizedDescription)"
        }
    }

    func restart() async {
        stopBackend()
        await start()
    }

    func stopBackend() {
        stopSharing()
        if let backendProcess {
            writeWrapperLog("stopping backend pid=\(backendProcess.processIdentifier)")
        }
        backendProcess?.terminate()
        backendProcess = nil
        currentPort = nil
        url = nil
        try? logHandle?.close()
        logHandle = nil
    }

    func stopSharing() {
        tailscaleProcess?.terminate()
        tailscaleProcess = nil
        sharingStatus = "Sharing is off."
    }

    func openInBrowser() {
        guard let url else { return }
        NSWorkspace.shared.open(url)
    }

    func openLogs() {
        guard let logURL else { return }
        NSWorkspace.shared.activateFileViewerSelecting([logURL])
    }

    private func waitForHealth(port: Int) async throws {
        for _ in 0..<1200 {
            if backendProcess?.isRunning == false {
                throw BackendError.backendExited
            }
            if await Self.canOpenLoopbackConnection(port: port) {
                return
            }
            try await Task.sleep(nanoseconds: 250_000_000)
        }
        throw BackendError.healthTimedOut
    }

    nonisolated private static func canOpenLoopbackConnection(port: Int) async -> Bool {
        await Task.detached(priority: .utility) {
            var hints = addrinfo()
            hints.ai_family = AF_INET
            hints.ai_socktype = SOCK_STREAM
            hints.ai_protocol = IPPROTO_TCP

            var addressInfo: UnsafeMutablePointer<addrinfo>?
            let status = getaddrinfo("127.0.0.1", "\(port)", &hints, &addressInfo)
            guard status == 0, let addressInfo else {
                return false
            }
            defer { freeaddrinfo(addressInfo) }

            let fd = socket(
                addressInfo.pointee.ai_family,
                addressInfo.pointee.ai_socktype,
                addressInfo.pointee.ai_protocol
            )
            guard fd >= 0 else {
                return false
            }
            defer { close(fd) }

            return Darwin.connect(
                fd,
                addressInfo.pointee.ai_addr,
                addressInfo.pointee.ai_addrlen
            ) == 0
        }.value
    }

    private func writeWrapperLog(_ message: String) {
        guard let data = "[wrapper] \(message)\n".data(using: .utf8) else {
            return
        }
        try? logHandle?.write(contentsOf: data)
    }

    private func syncSharingProcess() {
        stopSharing()
        guard sharingMode == .tailscaleServe else {
            sharingStatus = "Sharing is off. Odysseus is available only on this Mac."
            return
        }
        guard let port = currentPort else {
            sharingStatus = "Tailscale sharing will start after Odysseus starts."
            return
        }
        guard let tailscale = Self.findExecutable("tailscale") else {
            sharingStatus = "Tailscale CLI not found. Install Tailscale or add tailscale to PATH."
            return
        }

        let process = Process()
        process.executableURL = tailscale
        process.arguments = ["serve", "\(port)"]
        process.standardOutput = logHandle
        process.standardError = logHandle
        do {
            try process.run()
            tailscaleProcess = process
            sharingStatus = "Tailnet sharing is on via tailscale serve \(port)."
        } catch {
            sharingStatus = "Failed to start Tailscale sharing: \(error.localizedDescription)"
        }
    }

    private static func applicationSupportDirectory() throws -> URL {
        let base = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = base.appendingPathComponent("Odysseus", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private static func findFreePort() throws -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)
        guard fd >= 0 else {
            throw BackendError.noFreePort
        }
        defer { close(fd) }

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(0).bigEndian
        address.sin_addr = in_addr(s_addr: in_addr_t(INADDR_LOOPBACK).bigEndian)

        let bindResult = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                Darwin.bind(fd, socketAddress, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else {
            throw BackendError.noFreePort
        }

        var assignedAddress = sockaddr_in()
        var assignedLength = socklen_t(MemoryLayout<sockaddr_in>.size)
        let nameResult = withUnsafeMutablePointer(to: &assignedAddress) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                getsockname(fd, socketAddress, &assignedLength)
            }
        }
        guard nameResult == 0 else {
            throw BackendError.noFreePort
        }

        let port = UInt16(bigEndian: assignedAddress.sin_port)
        guard port > 0 else {
            throw BackendError.noFreePort
        }
        return Int(port)
    }

    private static func resolveBackendLaunch() throws -> BackendLaunch {
        if let bundled = Bundle.main.resourceURL?
            .appendingPathComponent("server", isDirectory: true)
            .appendingPathComponent("odysseus_backend"),
           FileManager.default.isExecutableFile(atPath: bundled.path) {
            return .bundled(executableURL: bundled, workingDirectory: bundled.deletingLastPathComponent())
        }

        let root = try resolveSourceBackendRoot()
        let python = try resolvePython(root: root)
        return .python(executableURL: python, backendRoot: root)
    }

    private static func resolveSourceBackendRoot() throws -> URL {
        if let override = ProcessInfo.processInfo.environment["ODYSSEUS_BACKEND_ROOT"], !override.isEmpty {
            let url = URL(fileURLWithPath: override)
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("app.py").path) {
                return url
            }
        }

        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
        var candidates = [
            cwd,
            cwd.deletingLastPathComponent(),
            cwd.deletingLastPathComponent().deletingLastPathComponent()
        ]
        if let bundleURL = Bundle.main.resourceURL {
            candidates.append(bundleURL)
            candidates.append(bundleURL.deletingLastPathComponent())
            candidates.append(bundleURL.deletingLastPathComponent().deletingLastPathComponent())
        }

        for candidate in candidates {
            if FileManager.default.fileExists(atPath: candidate.appendingPathComponent("app.py").path) {
                return candidate
            }
        }
        throw BackendError.backendRootNotFound
    }

    private static func resolvePython(root: URL) throws -> URL {
        let candidates = [
            ProcessInfo.processInfo.environment["ODYSSEUS_PYTHON"],
            root.appendingPathComponent(".venv/bin/python").path,
            root.appendingPathComponent("venv/bin/python").path,
            root.appendingPathComponent(".venv/bin/python3").path,
            root.appendingPathComponent("venv/bin/python3").path,
            findExecutable("python3")?.path,
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3"
        ].compactMap { $0 }

        for candidate in candidates {
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return URL(fileURLWithPath: candidate)
            }
        }
        throw BackendError.pythonNotFound
    }

    private static func findExecutable(_ name: String) -> URL? {
        let explicit = [
            "/opt/homebrew/bin/\(name)",
            "/usr/local/bin/\(name)",
            "/usr/bin/\(name)",
            "/bin/\(name)",
            "/Applications/Tailscale.app/Contents/MacOS/\(name)"
        ]
        for path in explicit where FileManager.default.isExecutableFile(atPath: path) {
            return URL(fileURLWithPath: path)
        }

        let path = ProcessInfo.processInfo.environment["PATH"] ?? ""
        for directory in path.split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory)).appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: candidate.path) {
                return candidate
            }
        }
        return nil
    }
}

private enum BackendLaunch {
    case bundled(executableURL: URL, workingDirectory: URL)
    case python(executableURL: URL, backendRoot: URL)

    var executableURL: URL {
        switch self {
        case .bundled(let executableURL, _), .python(let executableURL, _):
            return executableURL
        }
    }

    var workingDirectory: URL {
        switch self {
        case .bundled(_, let workingDirectory):
            return workingDirectory
        case .python(_, let backendRoot):
            return backendRoot
        }
    }

    func arguments(port: Int) -> [String] {
        switch self {
        case .bundled:
            return []
        case .python:
            return ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "\(port)"]
        }
    }

    func environment(port: Int, appSupport: URL, dataDir: URL, llmPorts: String) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
            env["PATH"] ?? ""
        ].joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        env["ODYSSEUS_DESKTOP"] = "1"
        env["ODYSSEUS_HOST"] = "127.0.0.1"
        env["ODYSSEUS_PORT"] = "\(port)"
        env["DATA_DIR"] = dataDir.path
        env["DATABASE_URL"] = "sqlite:///\(dataDir.appendingPathComponent("app.db").path)"
        env["CHROMADB_PERSIST_PATH"] = dataDir.appendingPathComponent("chroma", isDirectory: true).path
        env["FASTEMBED_CACHE_PATH"] = appSupport.appendingPathComponent("fastembed_cache", isDirectory: true).path
        env["LLM_HOST"] = "127.0.0.1"
        env["LLM_HOSTS"] = "127.0.0.1"
        env["LLM_PORTS"] = llmPorts

        if case .python(_, let backendRoot) = self {
            env["ODYSSEUS_BASE_DIR"] = backendRoot.path
        }
        return env
    }
}

enum BackendError: LocalizedError {
    case backendRootNotFound
    case pythonNotFound
    case noFreePort
    case healthTimedOut
    case backendExited

    var errorDescription: String? {
        switch self {
        case .backendRootNotFound:
            return "Could not find app.py. Set ODYSSEUS_BACKEND_ROOT to the Odysseus repo path."
        case .pythonNotFound:
            return "Could not find a Python interpreter. Set ODYSSEUS_PYTHON or create .venv/venv in the repo."
        case .noFreePort:
            return "Could not reserve a local port."
        case .healthTimedOut:
            return "The backend did not become healthy in time. Check the backend log."
        case .backendExited:
            return "The backend exited before it became healthy. Check the backend log."
        }
    }
}
