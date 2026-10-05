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

  /// Checks one image path and returns what to print and the exit code.
  ///
  /// - Parameter arguments: The command-line arguments without the program name: one image
  ///   path, optionally with `--json`.
  public func run(arguments: [String]) -> CommandResponse {
    let isJSON = arguments.contains("--json")
    let paths = arguments.filter { $0 != "--json" }
    guard arguments.filter({ $0 == "--json" }).count <= 1,
      paths.count == 1, !paths[0].hasPrefix("-")
    else {
      return failure(
        code: "usage", message: "Usage: retina-shrink <path> [--json]",
        exitCode: 2, isJSON: isJSON)
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
