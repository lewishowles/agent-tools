"""macOS clipboard support for page-to-markdown."""

import shutil
import subprocess
import sys


class ClipboardError(RuntimeError):
	"""Report that generated content could not be copied to the clipboard."""


def copy_to_clipboard(text: str) -> None:
	"""Copy text to the macOS clipboard using pbcopy."""
	if sys.platform != "darwin":
		raise ClipboardError("clipboard unavailable: pbcopy is only supported on macOS")

	if shutil.which("pbcopy") is None:
		raise ClipboardError("clipboard unavailable: pbcopy was not found on PATH")

	try:
		subprocess.run(["pbcopy"], input=text, text=True, check=True)
	except FileNotFoundError as error:
		raise ClipboardError(
			"clipboard unavailable: pbcopy was not found on PATH"
		) from error
	except OSError as error:
		raise ClipboardError(f"clipboard copy failed: {error}") from error
	except subprocess.CalledProcessError as error:
		raise ClipboardError(
			f"clipboard copy failed: pbcopy exited with status {error.returncode}"
		) from error
