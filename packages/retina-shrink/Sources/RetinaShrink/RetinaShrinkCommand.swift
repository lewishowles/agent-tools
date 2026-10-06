import AppKit
import Foundation

/// What one run of `retina-shrink` prints, and the exit code it ends with.
public struct CommandResponse {
  /// The path or JSON document for standard output.
  public let stdout: String
  /// A one-line error in plain mode, or nothing in JSON mode.
  public let stderr: String
  /// The exit code, following `docs/cli-contract.md`.
  public let exitCode: Int32
}

/// Turns command-line arguments into output and an exit code that follow `docs/cli-contract.md`.
///
/// Kept apart from `main.swift` so tests can check the output and exit codes without
/// starting a process.
public struct RetinaShrinkCommand {
  /// The shrinker that decides which image path to print.
  private let shrinker: RetinaShrinker

  /// Creates the command.
  ///
  /// - Parameter shrinker: The shrinker to use. Tests pass one with a temporary cache folder.
  public init(shrinker: RetinaShrinker = RetinaShrinker()) {
    self.shrinker = shrinker
  }

  /// Runs path mode or the on-demand clipboard command.
  ///
  /// - Parameter arguments: The command-line arguments without the program name: one image
  ///   path or `clipboard`, optionally with `--json`.
  public func run(arguments: [String]) -> CommandResponse {
    let isJSON = arguments.contains("--json")
    let paths = arguments.filter { $0 != "--json" }
    guard arguments.filter({ $0 == "--json" }).count <= 1,
      paths.count == 1, !paths[0].hasPrefix("-")
    else {
      return failure(
        code: "usage",
        message: "Usage: retina-shrink <path> [--json] | retina-shrink clipboard [--json]",
        exitCode: 2, isJSON: isJSON)
    }

    if paths[0] == "clipboard" {
      return runClipboard(isJSON: isJSON)
    }

    do {
      let result = try shrinker.shrink(path: paths[0])
      if !isJSON {
        return CommandResponse(stdout: "\(result.path)\n", stderr: "", exitCode: 0)
      }

      let encoder = JSONEncoder()
      encoder.keyEncodingStrategy = .convertToSnakeCase
      let data = try encoder.encode(SuccessEnvelope(ok: true, data: result))
      return CommandResponse(
        stdout: String(decoding: data, as: UTF8.self) + "\n",
        stderr: "", exitCode: 0)
    } catch ShrinkError.unreadable {
      return failure(
        code: "not-found", message: "Cannot read image file.",
        exitCode: 1, isJSON: isJSON)
    } catch ShrinkError.cannotWrite {
      return failure(
        code: "environment", message: "Cannot write cache image.",
        exitCode: 3, isJSON: isJSON)
    } catch {
      return failure(
        code: "internal", message: "Cannot process image.",
        exitCode: 3, isJSON: isJSON)
    }
  }

  /// Replaces a single copied image with a smaller PNG, and leaves anything else on the clipboard as it is.
  ///
  /// - Parameter isJSON: Whether to print the shared JSON envelope.
  private func runClipboard(isJSON: Bool) -> CommandResponse {
    let pasteboard = NSPasteboard.general
    // The clipboard's change count when it was read, used to spot a newer copy before writing.
    let changeCount = pasteboard.changeCount
    let decision = ClipboardImageDecision(shrinker: shrinker)
    guard let items = pasteboard.pasteboardItems, items.count == 1,
      let item = items.first
    else {
      return clipboardResponse(
        action: "unchanged", reason: "The clipboard does not contain one image.",
        image: nil, isJSON: isJSON)
    }

    let types = item.types
    guard decision.accepts(types: types.map(\.rawValue)) else {
      return clipboardResponse(
        action: "unchanged", reason: ClipboardImageDecision.unsupportedImageReason,
        image: nil, isJSON: isJSON)
    }

    let dataByType = Dictionary(
      uniqueKeysWithValues: types.compactMap { type in
        item.data(forType: type).map { (type.rawValue, $0) }
      })
    let imageItem = ClipboardImageItem(types: types.map(\.rawValue), dataByType: dataByType)

    do {
      switch try decision.decide(item: imageItem) {
      case .unchanged(let reason):
        return clipboardResponse(
          action: "unchanged", reason: reason, image: nil, isJSON: isJSON)
      case .image(let image):
        guard let outputData = image.outputData else {
          return clipboardResponse(
            action: "unchanged", reason: "Shrinking would not make the image smaller.",
            image: image, isJSON: isJSON)
        }

        let replacement = NSPasteboardItem()
        // A copy of the original item, put back if writing the smaller image fails.
        let backup = NSPasteboardItem()
        guard replacement.setData(outputData, forType: .png),
          types.allSatisfy({ type in
            guard let data = dataByType[type.rawValue] else { return false }
            return backup.setData(data, forType: type)
          })
        else {
          return failure(
            code: "environment", message: "Cannot prepare the clipboard image.",
            exitCode: 3, isJSON: isJSON)
        }

        // Leave a newer copy alone if the user copied something else while the image was shrinking.
        guard pasteboard.changeCount == changeCount else {
          return clipboardResponse(
            action: "unchanged", reason: "The clipboard changed while the image was checked.",
            image: image, isJSON: isJSON)
        }

        pasteboard.clearContents()
        guard pasteboard.writeObjects([replacement]) else {
          // Restore only an empty clipboard, so a newer copy is not overwritten.
          var wasRestored = false
          if pasteboard.pasteboardItems?.isEmpty ?? true {
            pasteboard.clearContents()
            wasRestored = pasteboard.writeObjects([backup])
          }
          return failure(
            code: "environment",
            message: wasRestored
              ? "Cannot write the shrunk image. The original image is still on the clipboard."
              : "Cannot write the shrunk image. The original image could not be put back; copy it again.",
            exitCode: 3, isJSON: isJSON)
        }

        return clipboardResponse(action: "shrunk", reason: nil, image: image, isJSON: isJSON)
      }
    } catch {
      return failure(
        code: "internal", message: "Cannot process the clipboard image.",
        exitCode: 3, isJSON: isJSON)
    }
  }

  /// Builds the output for one clipboard run: one line of text, or the JSON envelope.
  ///
  /// - Parameters:
  ///   - action: `shrunk` when the clipboard was replaced, or `unchanged` otherwise.
  ///   - reason: Why the clipboard was left alone, if it was.
  ///   - image: The decoded image sizes, when an image could be checked.
  ///   - isJSON: Whether to print the shared JSON envelope.
  private func clipboardResponse(
    action: String, reason: String?, image: ImageShrinkResult?, isJSON: Bool
  ) -> CommandResponse {
    if !isJSON {
      if let reason {
        return CommandResponse(stdout: "Clipboard unchanged: \(reason)\n", stderr: "", exitCode: 0)
      }

      guard let image else {
        return failure(
          code: "internal", message: "Cannot report the clipboard image.",
          exitCode: 3, isJSON: false)
      }

      return CommandResponse(
        stdout:
          "Clipboard image shrunk from \(image.originalWidth) × \(image.originalHeight) to \(image.outputWidth) × \(image.outputHeight) pixels.\n",
        stderr: "", exitCode: 0)
    }

    let result = ClipboardResult(
      action: action, reason: reason,
      originalWidth: image?.originalWidth, originalHeight: image?.originalHeight,
      outputWidth: image?.outputWidth, outputHeight: image?.outputHeight,
      originalBytes: image?.originalBytes, outputBytes: image?.outputBytes)
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    guard let data = try? encoder.encode(SuccessEnvelope(ok: true, data: result)) else {
      return failure(
        code: "internal", message: "Cannot encode the clipboard result.",
        exitCode: 3, isJSON: true)
    }
    return CommandResponse(
      stdout: String(decoding: data, as: UTF8.self) + "\n", stderr: "", exitCode: 0)
  }

  /// Builds the error output: one line on stderr in plain mode, or the error envelope on stdout with `--json`.
  ///
  /// - Parameters:
  ///   - code: The error code from `docs/cli-contract.md`.
  ///   - message: The one-line reason shown to the user.
  ///   - exitCode: The exit code that goes with `code`.
  ///   - isJSON: Whether the user passed `--json`.
  private func failure(
    code: String, message: String, exitCode: Int32, isJSON: Bool
  ) -> CommandResponse {
    if !isJSON {
      return CommandResponse(stdout: "", stderr: "retina-shrink: \(message)\n", exitCode: exitCode)
    }

    let envelope = FailureEnvelope(ok: false, error: ErrorDetails(code: code, message: message))
    let data = try? JSONEncoder().encode(envelope)
    let text =
      data.map { String(decoding: $0, as: UTF8.self) }
      ?? #"{"ok":false,"error":{"code":"internal","message":"Cannot process image."}}"#
    return CommandResponse(stdout: text + "\n", stderr: "", exitCode: exitCode)
  }
}

/// The clipboard action and image sizes printed with `--json`.
private struct ClipboardResult: Encodable {
  /// `shrunk` when the clipboard was replaced, or `unchanged`.
  let action: String
  /// Why the clipboard was left alone, when no replacement was made.
  let reason: String?
  /// The width of the copied image, when it could be decoded.
  let originalWidth: Int?
  /// The height of the copied image, when it could be decoded.
  let originalHeight: Int?
  /// The width of the image left on the clipboard, when the copied image could be decoded.
  let outputWidth: Int?
  /// The height of the image left on the clipboard, when the copied image could be decoded.
  let outputHeight: Int?
  /// The number of bytes in the copied image, when it could be decoded.
  let originalBytes: Int?
  /// The number of bytes in the image left on the clipboard, when the copied image could be decoded.
  let outputBytes: Int?
}

/// The shared JSON wrapper for a successful command.
private struct SuccessEnvelope<Payload: Encodable>: Encodable {
  /// Whether the command succeeded.
  let ok: Bool
  /// The command-specific result.
  let data: Payload
}

/// The shared JSON wrapper for a failed command.
private struct FailureEnvelope: Encodable {
  /// Whether the command succeeded.
  let ok: Bool
  /// The reason the command failed.
  let error: ErrorDetails
}

/// The machine-readable code and human-readable reason for a failure.
private struct ErrorDetails: Encodable {
  /// The stable error code from the CLI contract.
  let code: String
  /// The reason the command could not return a path.
  let message: String
}
