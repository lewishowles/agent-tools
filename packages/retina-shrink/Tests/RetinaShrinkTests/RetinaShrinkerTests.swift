import CoreGraphics
import Foundation
import ImageIO
import RetinaShrink
import Testing
import UniformTypeIdentifiers

/// Checks path mode using image files created during each test.
@Suite struct RetinaShrinkerTests {
  /// A Retina image yields a half-size PNG and reuses it on the next call.
  @Test func halvesRetinaImage() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("retina.png")
    try makeImage(at: source, width: 512, height: 512, dpi: 144, noisy: true)

    let sourceBytes = try Data(contentsOf: source).count
    #expect(sourceBytes > 200_000)

    let shrinker = RetinaShrinker(cacheDirectory: directory.appendingPathComponent("cache"))
    let result = try shrinker.shrink(path: source.path)

    #expect(result.halved)
    #expect(result.outputWidth == 256)
    #expect(result.outputHeight == 256)
    #expect(result.outputBytes < result.originalBytes)
    #expect(result.path.hasSuffix(".png"))
    #expect(FileManager.default.fileExists(atPath: result.path))

    let outputSource = try #require(
      CGImageSourceCreateWithURL(
        URL(fileURLWithPath: result.path) as CFURL, nil))
    let properties = CGImageSourceCopyPropertiesAtIndex(outputSource, 0, nil) as? [CFString: Any]
    #expect((properties?[kCGImagePropertyDPIWidth] as? NSNumber)?.intValue == 72)
    #expect((properties?[kCGImagePropertyDPIHeight] as? NSNumber)?.intValue == 72)

    let repeated = try shrinker.shrink(path: source.path)
    #expect(repeated.path == result.path)
    #expect(repeated.outputBytes == result.outputBytes)
  }

  /// A halved Display P3 image keeps its colour profile in the PNG.
  @Test func keepsDisplayP3ColourSpace() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("display-p3.png")
    try makeDisplayP3Image(at: source)

    let inputSource = CGImageSourceCreateWithURL(source as CFURL, nil)
    let inputImage = inputSource.flatMap { CGImageSourceCreateImageAtIndex($0, 0, nil) }
    #expect(inputImage?.colorSpace?.name == CGColorSpace.displayP3)

    let result = try RetinaShrinker(cacheDirectory: directory.appendingPathComponent("cache"))
      .shrink(path: source.path)

    #expect(result.halved)
    #expect(result.outputBytes < result.originalBytes)

    let outputURL = URL(fileURLWithPath: result.path)
    let outputSource = CGImageSourceCreateWithURL(outputURL as CFURL, nil)
    let outputImage = outputSource.flatMap { CGImageSourceCreateImageAtIndex($0, 0, nil) }
    #expect(outputImage?.colorSpace?.name == CGColorSpace.displayP3)
  }

  /// An EXIF-rotated image is upright after halving.
  @Test func appliesImageOrientation() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("rotated.jpg")
    try makeRotatedImage(at: source)

    let inputSource = try #require(CGImageSourceCreateWithURL(source as CFURL, nil))
    let properties = CGImageSourceCopyPropertiesAtIndex(inputSource, 0, nil) as? [CFString: Any]
    #expect((properties?[kCGImagePropertyOrientation] as? NSNumber)?.intValue == 6)

    let result = try RetinaShrinker(cacheDirectory: directory.appendingPathComponent("cache"))
      .shrink(path: source.path)

    #expect(result.halved)
    #expect(result.originalWidth == 400)
    #expect(result.originalHeight == 200)
    #expect(result.outputWidth == 100)
    #expect(result.outputHeight == 200)
  }

  /// An ordinary image keeps its own path and creates no cached copy.
  @Test func keepsNonRetinaImage() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("ordinary.png")
    let cache = directory.appendingPathComponent("cache")
    try makeImage(at: source, width: 64, height: 64, dpi: 72, noisy: true)

    let result = try RetinaShrinker(cacheDirectory: cache).shrink(path: source.path)

    #expect(!result.halved)
    #expect(result.path == source.path)
    #expect(result.originalBytes == result.outputBytes)
    #expect(!FileManager.default.fileExists(atPath: cache.path))
  }

  /// A tiny Retina image keeps its own path when its halved copy would not be smaller.
  @Test func keepsImageWithoutByteSavings() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("tiny.png")
    try makeImage(at: source, width: 2, height: 2, dpi: 144, noisy: false)

    let result = try RetinaShrinker(cacheDirectory: directory.appendingPathComponent("cache"))
      .shrink(path: source.path)

    #expect(!result.halved)
    #expect(result.path == source.path)
    #expect(result.originalBytes == result.outputBytes)
  }

  /// A missing input reports an error instead of a path.
  @Test func rejectsMissingFile() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let missing = directory.appendingPathComponent("missing.png")
    #expect(throws: ShrinkError.self) {
      try RetinaShrinker(cacheDirectory: directory).shrink(path: missing.path)
    }
  }

  /// Plain mode prints only the path to open, which is what the Claude Code hook reads.
  @Test func printsPlainPath() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("ordinary.png")
    try makeImage(at: source, width: 64, height: 64, dpi: 72, noisy: true)

    let response = RetinaShrinkCommand(shrinker: RetinaShrinker(cacheDirectory: directory))
      .run(arguments: [source.path])

    #expect(response.stdout == "\(source.path)\n")
    #expect(response.stderr.isEmpty)
    #expect(response.exitCode == 0)
  }

  /// JSON mode wraps the result and uses snake_case size keys.
  @Test func printsJSONResult() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("ordinary.png")
    try makeImage(at: source, width: 64, height: 64, dpi: 72, noisy: true)

    let response = RetinaShrinkCommand(shrinker: RetinaShrinker(cacheDirectory: directory))
      .run(arguments: [source.path, "--json"])
    let document = try JSONSerialization.jsonObject(with: Data(response.stdout.utf8))
    let envelope = try #require(document as? [String: Any])
    let data = try #require(envelope["data"] as? [String: Any])

    #expect(response.exitCode == 0)
    #expect(response.stderr.isEmpty)
    #expect((envelope["ok"] as? Bool) == true)
    #expect(envelope["error"] == nil)
    #expect((data["path"] as? String) == source.path)
    #expect((data["halved"] as? Bool) == false)
    #expect((data["original_width"] as? Int) == 64)
    #expect((data["output_height"] as? Int) == 64)
    #expect((data["original_bytes"] as? Int) == (data["output_bytes"] as? Int))
  }

  /// Usage and missing files use the shared error envelope and exit codes.
  @Test func reportsCLIInputErrors() throws {
    let command = RetinaShrinkCommand()
    let usage = command.run(arguments: ["--json"])
    let missing = command.run(arguments: ["--json", "/path/that/does/not/exist.png"])

    let usageDocument = try JSONSerialization.jsonObject(with: Data(usage.stdout.utf8))
    let usageEnvelope = try #require(usageDocument as? [String: Any])
    let usageError = try #require(usageEnvelope["error"] as? [String: Any])
    let missingDocument = try JSONSerialization.jsonObject(with: Data(missing.stdout.utf8))
    let missingEnvelope = try #require(missingDocument as? [String: Any])
    let missingError = try #require(missingEnvelope["error"] as? [String: Any])

    #expect(usage.exitCode == 2)
    #expect(usage.stderr.isEmpty)
    #expect((usageEnvelope["ok"] as? Bool) == false)
    #expect((usageError["code"] as? String) == "usage")
    #expect(usageError["message"] is String)
    #expect(missing.exitCode == 1)
    #expect(missing.stderr.isEmpty)
    #expect((missingEnvelope["ok"] as? Bool) == false)
    #expect((missingError["code"] as? String) == "not-found")
    #expect(missingError["message"] is String)

    let plainMissing = command.run(arguments: ["/path/that/does/not/exist.png"])
    #expect(plainMissing.exitCode == 1)
    #expect(plainMissing.stdout.isEmpty)
    #expect(plainMissing.stderr == "retina-shrink: Cannot read image file.\n")
  }

  /// An unwritable cache reports an environment error after finding a smaller image.
  @Test func reportsCacheWriteError() throws {
    let directory = try temporaryDirectory()
    defer { try? FileManager.default.removeItem(at: directory) }

    let source = directory.appendingPathComponent("retina.png")
    let blockedCache = directory.appendingPathComponent("blocked-cache")
    try makeImage(at: source, width: 512, height: 512, dpi: 144, noisy: true)
    try Data("occupied".utf8).write(to: blockedCache)

    let response = RetinaShrinkCommand(
      shrinker: RetinaShrinker(cacheDirectory: blockedCache)
    ).run(arguments: ["--json", source.path])
    let document = try JSONSerialization.jsonObject(with: Data(response.stdout.utf8))
    let envelope = try #require(document as? [String: Any])
    let error = try #require(envelope["error"] as? [String: Any])

    #expect(response.exitCode == 3)
    #expect(response.stderr.isEmpty)
    #expect((envelope["ok"] as? Bool) == false)
    #expect((error["code"] as? String) == "environment")
  }

  /// Gives each test an isolated location for source and cached files.
  private func temporaryDirectory() throws -> URL {
    let directory = FileManager.default.temporaryDirectory
      .appendingPathComponent(UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    return directory
  }

  /// Writes a greyscale PNG with the given size and DPI.
  ///
  /// - Parameters:
  ///   - url: Where to write the PNG.
  ///   - width: The image width in pixels.
  ///   - height: The image height in pixels.
  ///   - dpi: The DPI recorded in the file, for both directions.
  ///   - noisy: Whether to fill the image with varied pixels. Varied pixels compress badly,
  ///     so the file is large enough for a halved copy to be smaller. Otherwise every pixel is black.
  private func makeImage(
    at url: URL, width: Int, height: Int, dpi: Int, noisy: Bool
  ) throws {
    var pixels = [UInt8](repeating: 0, count: width * height)
    if noisy {
      var seed: UInt32 = 0x1234_5678
      for index in pixels.indices {
        seed ^= seed << 13
        seed ^= seed >> 17
        seed ^= seed << 5
        pixels[index] = UInt8(truncatingIfNeeded: seed)
      }
    }

    let provider = try #require(CGDataProvider(data: Data(pixels) as CFData))
    let image = try #require(
      CGImage(
        width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 8,
        bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
        provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
      ))
    let destination = try #require(
      CGImageDestinationCreateWithURL(
        url as CFURL, UTType.png.identifier as CFString, 1, nil))

    let properties: [CFString: Any] = [
      kCGImagePropertyDPIWidth: dpi,
      kCGImagePropertyDPIHeight: dpi,
    ]
    CGImageDestinationAddImage(destination, image, properties as CFDictionary)
    try #require(CGImageDestinationFinalize(destination))
  }

  /// Writes a varied Retina PNG in the Display P3 colour space.
  private func makeDisplayP3Image(at url: URL) throws {
    let width = 512
    let height = 512
    var pixels = [UInt8](repeating: 255, count: width * height * 4)
    var seed: UInt32 = 0x8765_4321
    for pixel in 0..<(width * height) {
      for channel in 0..<3 {
        seed ^= seed << 13
        seed ^= seed >> 17
        seed ^= seed << 5
        pixels[pixel * 4 + channel] = UInt8(truncatingIfNeeded: seed)
      }
    }

    let colourSpace = try #require(CGColorSpace(name: CGColorSpace.displayP3))
    let provider = try #require(CGDataProvider(data: Data(pixels) as CFData))
    let image = try #require(
      CGImage(
        width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
        bytesPerRow: width * 4, space: colourSpace,
        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
        provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
      ))
    let destination = try #require(
      CGImageDestinationCreateWithURL(
        url as CFURL, UTType.png.identifier as CFString, 1, nil))

    let properties: [CFString: Any] = [
      kCGImagePropertyDPIWidth: 144,
      kCGImagePropertyDPIHeight: 144,
    ]
    CGImageDestinationAddImage(destination, image, properties as CFDictionary)
    try #require(CGImageDestinationFinalize(destination))
  }

  /// Writes a JPEG whose stored pixels need a quarter turn to display upright.
  private func makeRotatedImage(at url: URL) throws {
    let width = 400
    let height = 200
    var pixels = [UInt8](repeating: 0, count: width * height * 3)
    var seed: UInt32 = 0x2468_ace0
    for index in pixels.indices {
      seed ^= seed << 13
      seed ^= seed >> 17
      seed ^= seed << 5
      pixels[index] = UInt8(truncatingIfNeeded: seed)
    }

    let provider = try #require(CGDataProvider(data: Data(pixels) as CFData))
    let image = try #require(
      CGImage(
        width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 24,
        bytesPerRow: width * 3, space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
        provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent
      ))
    let destination = try #require(
      CGImageDestinationCreateWithURL(
        url as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
    let properties: [CFString: Any] = [
      kCGImagePropertyDPIWidth: 144,
      kCGImagePropertyDPIHeight: 144,
      kCGImagePropertyOrientation: 6,
      kCGImageDestinationLossyCompressionQuality: 1.0,
    ]
    CGImageDestinationAddImage(destination, image, properties as CFDictionary)
    try #require(CGImageDestinationFinalize(destination))
  }
}
