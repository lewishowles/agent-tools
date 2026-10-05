import Foundation
import RetinaShrink

let response = RetinaShrinkCommand().run(arguments: Array(CommandLine.arguments.dropFirst()))
FileHandle.standardOutput.write(Data(response.stdout.utf8))
FileHandle.standardError.write(Data(response.stderr.utf8))
exit(response.exitCode)
