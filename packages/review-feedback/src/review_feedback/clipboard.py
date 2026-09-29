"""Read from and write to the macOS clipboard via pbpaste and pbcopy."""

import re
import shutil
import subprocess
import sys


class ClipboardError(RuntimeError):
	"""Report that macOS clipboard access is unavailable or failed."""


def strip_clipboard_gutter(selection: str) -> str:
	"""Remove bat and delta line gutters without changing source indentation."""
	return re.sub(r"(?m)^ *\d+ [-+~ ] ?", "", selection)


def read_clipboard() -> str:
	"""Read text from the macOS clipboard using pbpaste."""
	if sys.platform != "darwin":
		raise ClipboardError(
			"clipboard unavailable: this version supports macOS only; "
			"copy the selection manually on macOS and run `review-feedback add`"
		)

	if shutil.which("pbpaste") is None:
		raise ClipboardError(
			"clipboard unavailable: pbpaste was not found on PATH; "
			"copy the selection manually and run `review-feedback add`"
		)

	try:
		result = subprocess.run(
			["pbpaste"],
			capture_output=True,
			check=True,
		)
	except FileNotFoundError as error:
		raise ClipboardError(
			"clipboard unavailable: pbpaste was not found on PATH; "
			"copy the selection manually and run `review-feedback add`"
		) from error
	except OSError as error:
		raise ClipboardError(
			f"clipboard read failed: {error}; copy the selection manually and "
			"run `review-feedback add`"
		) from error
	except subprocess.CalledProcessError as error:
		raise ClipboardError(
			f"clipboard read failed: pbpaste exited with status {error.returncode}; "
			"copy the selection manually and run `review-feedback add`"
		) from error

	try:
		selection = result.stdout.decode("utf-8")
	except UnicodeDecodeError as error:
		raise ClipboardError(
			"clipboard content is not readable UTF-8; copy plain text and "
			"run `review-feedback add`"
		) from error

	if selection == "":
		raise ClipboardError(
			"clipboard is empty; copy one source selection and run `review-feedback add`"
		)

	return selection


def copy_to_clipboard(text: str) -> None:
	"""Copy text to the macOS clipboard using pbcopy."""
	if sys.platform != "darwin":
		raise ClipboardError(
			"clipboard unavailable: this version supports macOS only; "
			"save the packet from standard output instead"
		)

	if shutil.which("pbcopy") is None:
		raise ClipboardError(
			"clipboard unavailable: pbcopy was not found on PATH; "
			"save the packet from standard output instead"
		)

	try:
		subprocess.run(["pbcopy"], input=text, text=True, check=True)
	except FileNotFoundError as error:
		raise ClipboardError(
			"clipboard unavailable: pbcopy was not found on PATH; "
			"save the packet from standard output instead"
		) from error
	except OSError as error:
		raise ClipboardError(
			f"clipboard copy failed: {error}; save the packet from standard output "
			"instead"
		) from error
	except subprocess.CalledProcessError as error:
		raise ClipboardError(
			f"clipboard copy failed: pbcopy exited with status {error.returncode}; "
			"save the packet from standard output instead"
		) from error
