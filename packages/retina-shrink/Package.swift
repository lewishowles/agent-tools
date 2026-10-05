// swift-tools-version: 6.0
import PackageDescription

let package = Package(
  name: "retina-shrink",
  platforms: [.macOS(.v13)],
  products: [
    .executable(name: "retina-shrink", targets: ["RetinaShrinkCLI"])
  ],
  targets: [
    .target(name: "RetinaShrink"),
    .executableTarget(name: "RetinaShrinkCLI", dependencies: ["RetinaShrink"]),
    .testTarget(name: "RetinaShrinkTests", dependencies: ["RetinaShrink"]),
  ]
)
