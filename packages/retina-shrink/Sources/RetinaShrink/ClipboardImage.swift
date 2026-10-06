import AppKit
import Foundation

/// The declared pasteboard types and their bytes for one copied item.
public struct ClipboardImageItem {
  /// Every type declared by the copied item.
  public let types: [String]
  /// The bytes for each declared type that the item can provide.
  public let dataByType: [String: Data]

  /// Creates an item from its types and bytes, so it can be checked without the system clipboard.
  ///
  /// - Parameters:
  ///   - types: Every type the item declares.
  ///   - dataByType: The bytes supplied for those types.
  public init(types: [String], dataByType: [String: Data]) {
    self.types = types
    self.dataByType = dataByType
  }
}

/// The outcome of checking one copied item without changing the clipboard.
public enum ClipboardImageOutcome {
  /// The item must stay as it is, with a reason to show the user.
  case unchanged(String)
  /// The image was decoded; its output data is nil when a dimension is smaller than the
  /// scale factor or shrinking would not save bytes.
  case image(ImageShrinkResult)
}

/// Decides what the shared shrink rule makes of one copied image item.
///
/// An embedded image may carry a file link or private types. A file link without image data
/// may carry only its own name or path as text, and its icon.
public struct ClipboardImageDecision {
  /// The reason shown when a copied item has types outside the supported image shapes.
  static let unsupportedImageReason = "The clipboard does not contain only an image."

  /// The image rule shared with path mode.
  private let shrinker: RetinaShrinker

  /// Creates a decision that applies the given image rule.
  ///
  /// - Parameter shrinker: The image rule shared with path mode.
  public init(shrinker: RetinaShrinker = RetinaShrinker()) {
    self.shrinker = shrinker
  }

  /// Returns true when an item's declared types fit either supported image shape.
  ///
  /// The command calls this before reading data, so other clipboard content is never read.
  ///
  /// - Parameter types: Every type declared by one copied item.
  public func accepts(types: [String]) -> Bool {
    let hasImageData =
      types.contains(NSPasteboard.PasteboardType.png.rawValue)
      || types.contains(NSPasteboard.PasteboardType.tiff.rawValue)
    if hasImageData {
      return types.allSatisfy {
        $0 == NSPasteboard.PasteboardType.png.rawValue
          || $0 == NSPasteboard.PasteboardType.tiff.rawValue
          || $0 == NSPasteboard.PasteboardType.fileURL.rawValue
          || $0.hasPrefix("dyn.")
      }
    }

    return types.contains(NSPasteboard.PasteboardType.fileURL.rawValue)
      && types.allSatisfy {
        $0 == NSPasteboard.PasteboardType.fileURL.rawValue
          || $0 == "public.utf16-external-plain-text"
          || $0 == NSPasteboard.PasteboardType.string.rawValue
          || $0 == "com.apple.icns"
      }
  }

  /// Applies the image rule to one copied item without using the system clipboard.
  ///
  /// The PNG data is used when the item has both PNG and TIFF. A linked file is read only
  /// when there is no embedded image data.
  ///
  /// - Parameter item: One copied item with every type it declares.
  /// - Returns: A reason to leave the item unchanged, or the image and its shrunk data.
  /// - Throws: `ShrinkError.cannotWrite` when ImageIO cannot encode the PNG.
  public func decide(item: ClipboardImageItem) throws -> ClipboardImageOutcome {
    guard accepts(types: item.types) else {
      return .unchanged(Self.unsupportedImageReason)
    }

    guard item.types.allSatisfy({ item.dataByType[$0] != nil }) else {
      return .unchanged("The clipboard image could not be read.")
    }

    let embeddedImageData =
      item.dataByType[NSPasteboard.PasteboardType.png.rawValue]
      ?? item.dataByType[NSPasteboard.PasteboardType.tiff.rawValue]
    let imageData: Data

    if let embeddedImageData {
      imageData = embeddedImageData
    } else {
      guard let urlData = item.dataByType[NSPasteboard.PasteboardType.fileURL.rawValue],
        let fileURL = URL(dataRepresentation: urlData, relativeTo: nil), fileURL.isFileURL
      else {
        return .unchanged("The clipboard image could not be read.")
      }

      // Finder adds the file's name or path as text. Any other text means more than the file
      // was copied, so the clipboard is left alone.
      for type in item.types {
        let encoding: String.Encoding
        switch type {
        case "public.utf16-external-plain-text":
          encoding = .utf16
        case NSPasteboard.PasteboardType.string.rawValue:
          encoding = .utf8
        default:
          continue
        }

        guard let textData = item.dataByType[type],
          let text = String(data: textData, encoding: encoding),
          text == fileURL.lastPathComponent || text == fileURL.path
        else {
          return .unchanged("The clipboard contains text besides the image file.")
        }
      }

      guard let fileData = try? Data(contentsOf: fileURL) else {
        return .unchanged("The clipboard image file could not be read.")
      }

      imageData = fileData
    }

    do {
      return .image(
        try shrinker.shrink(
          data: imageData, retinaScaleFactor: 4, otherScaleFactor: 2))
    } catch ShrinkError.unreadable {
      return .unchanged("The clipboard image could not be read.")
    }
  }
}
