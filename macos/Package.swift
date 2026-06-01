// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "OdysseusMac",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "Odysseus", targets: ["OdysseusMac"])
    ],
    targets: [
        .executableTarget(name: "OdysseusMac")
    ]
)
