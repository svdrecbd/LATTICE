// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LatticeCLI",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "lattice", targets: ["LatticeCLI"])
    ],
    targets: [
        .executableTarget(
            name: "LatticeCLI",
            path: "Sources/LatticeCLI"
        )
    ]
)
