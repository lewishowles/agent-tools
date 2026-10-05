import CoreGraphics
import CryptoKit
import Foundation
import ImageIO
import UniformTypeIdentifiers

/// The image an agent should open, with the sizes before and after shrinking.
public struct ShrinkResult: Encodable {
  /// The path the agent should open: the halved copy, or the original image.
  public let path: String
  /// Whether `path` points to a halved copy rather than the original image.
  public let halved: Bool
  /// The size of the original file in bytes.
  public let originalBytes: Int
  /// The size of the file at `path` in bytes.
  public let outputBytes: Int
  /// The width of the original image in pixels, as stored in the file.
  ///
  /// For a photo with a rotation tag, this is the stored width, before the image is turned
  /// upright, so it can match `outputHeight` rather than `outputWidth`.
  public let originalWidth: Int
  /// The height of the original image in pixels, as stored in the file.
  public let originalHeight: Int
  /// The width of the image at `path` in pixels, after turning it upright.
  public let outputWidth: Int
  /// The height of the image at `path` in pixels, after turning it upright.
  public let outputHeight: Int
}

/// The reasons path mode can fail to give back a path.
public enum ShrinkError: Error {
  /// The file is missing, or its contents are not an image macOS can decode.
  case unreadable
  /// The halved copy could not be encoded or saved to the cache.
  case cannotWrite
}

/// Halves Retina images so an agent opens the smaller copy, and leaves every other image alone.
public struct RetinaShrinker {
  /// The folder that holds halved copies, so the same image is only halved once.
  private let cacheDirectory: URL

  /// Creates a shrinker that saves halved copies in the user's cache folder.
  ///
  /// - Parameter cacheDirectory: The folder for halved copies. Tests pass a temporary folder.
  public init(
    cacheDirectory: URL = FileManager.default.homeDirectoryForCurrentUser
      .appendingPathComponent("Library/Caches/retina-shrink", isDirectory: true)
  ) {
    self.cacheDirectory = cacheDirectory
  }

  /// Returns a halved PNG copy of a Retina image, or the original path when halving would not help.
  ///
  /// An image counts as Retina when both its horizontal and vertical DPI are 144 or more.
  /// The original path comes back for any other image, and for a Retina image whose halved
  /// copy would not be smaller in bytes.
  ///
  /// - Parameter path: The image file to check.
  /// - Throws: `ShrinkError.unreadable` when the file cannot be read as an image, or
  ///   `ShrinkError.cannotWrite` when the halved copy cannot be encoded or saved.
  public func shrink(path: String) throws -> ShrinkResult {
    let sourceURL = URL(fileURLWithPath: path)
    guard let sourceData = try? Data(contentsOf: sourceURL),
      let imageSource = CGImageSourceCreateWithData(sourceData as CFData, nil),
      let image = CGImageSourceCreateImageAtIndex(imageSource, 0, nil)
    else {
      throw ShrinkError.unreadable
    }

    let width = image.width
    let height = image.height
    let original = ShrinkResult(
      path: path, halved: false,
      originalBytes: sourceData.count, outputBytes: sourceData.count,
      originalWidth: width, originalHeight: height,
      outputWidth: width, outputHeight: height
    )

    let properties = CGImageSourceCopyPropertiesAtIndex(imageSource, 0, nil) as? [CFString: Any]
    let horizontalDPI = (properties?[kCGImagePropertyDPIWidth] as? NSNumber)?.doubleValue ?? 0
    let verticalDPI = (properties?[kCGImagePropertyDPIHeight] as? NSNumber)?.doubleValue ?? 0
    guard horizontalDPI >= 144, verticalDPI >= 144, width >= 2, height >= 2 else {
      return original
    }

    let digest = SHA256.hash(data: sourceData).map { String(format: "%02x", $0) }.joined()
    let outputURL = cacheDirectory.appendingPathComponent("\(digest).png")

    if let cachedData = try? Data(contentsOf: outputURL),
      let cachedSource = CGImageSourceCreateWithData(cachedData as CFData, nil),
      let cachedImage = CGImageSourceCreateImageAtIndex(cachedSource, 0, nil)
    {
      return ShrinkResult(
        path: outputURL.path, halved: true,
        originalBytes: sourceData.count, outputBytes: cachedData.count,
        originalWidth: width, originalHeight: height,
        outputWidth: cachedImage.width, outputHeight: cachedImage.height
      )
    }

    // ImageIO applies EXIF orientation and carries the source colour profile into the PNG.
    let thumbnailOptions: [CFString: Any] = [
      kCGImageSourceCreateThumbnailFromImageAlways: true,
      kCGImageSourceCreateThumbnailWithTransform: true,
      kCGImageSourceThumbnailMaxPixelSize: max(width, height) / 2,
    ]
    guard
      let scaledImage = CGImageSourceCreateThumbnailAtIndex(
        imageSource, 0, thumbnailOptions as CFDictionary
      )
    else {
      return original
    }

    let outputWidth = scaledImage.width
    let outputHeight = scaledImage.height

    let pngData = NSMutableData()
    guard
      let destination = CGImageDestinationCreateWithData(
        pngData, UTType.png.identifier as CFString, 1, nil
      )
    else {
      throw ShrinkError.cannotWrite
    }

    let outputProperties: [CFString: Any] = [
      kCGImagePropertyDPIWidth: 72,
      kCGImagePropertyDPIHeight: 72,
    ]
    CGImageDestinationAddImage(destination, scaledImage, outputProperties as CFDictionary)
    guard CGImageDestinationFinalize(destination) else {
      throw ShrinkError.cannotWrite
    }

    let outputData = pngData as Data
    guard outputData.count < sourceData.count else {
      return original
    }

    do {
      try FileManager.default.createDirectory(
        at: cacheDirectory, withIntermediateDirectories: true
      )
      try outputData.write(to: outputURL, options: .atomic)
    } catch {
      throw ShrinkError.cannotWrite
    }

    return ShrinkResult(
      path: outputURL.path, halved: true,
      originalBytes: sourceData.count, outputBytes: outputData.count,
      originalWidth: width, originalHeight: height,
      outputWidth: outputWidth, outputHeight: outputHeight
    )
  }
}
