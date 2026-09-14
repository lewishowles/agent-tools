"""Write agent-run results as text or as the shared JSON envelope."""

import json
import sys
from typing import TextIO

# Exit status for each error code in the shared CLI contract.
_EXIT_CODES = {
    "usage": 2,
    "not-found": 1,
    "check-failed": 1,
    "environment": 3,
    "internal": 3,
}


def render_success(
    *,
    json_mode: bool,
    data: object | None = None,
    text: str | None = None,
    diagnostic: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Write a successful result and return exit status 0.

    Args:
        json_mode: Write `{"ok": true, "data": ...}` to stdout instead of text.
        data: Result sent in JSON mode.
        text: Human-readable result sent to stdout in text mode. Nothing is
            written when it is empty.
        diagnostic: Extra detail sent to stderr in JSON mode only, so stdout
            stays a single JSON document.
        stdout: Stream for the result; defaults to `sys.stdout`.
        stderr: Stream for the diagnostic; defaults to `sys.stderr`.
    """
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr

    if json_mode:
        payload = {"ok": True, "data": data}
        print(json.dumps(payload), file=output_stream)

        if diagnostic:
            print(diagnostic, file=error_stream)
    elif text:
        print(text, file=output_stream, end="" if text.endswith("\n") else "\n")

    return 0


def render_error(
    *,
    json_mode: bool,
    code: str,
    message: str,
    text: str | None = None,
    diagnostic: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Write a failed result and return the exit status for its error code.

    In JSON mode the error envelope goes to stdout, so callers can always parse
    stdout; in text mode the error goes to stderr.

    Args:
        json_mode: Write `{"ok": false, "error": {...}}` instead of text.
        code: Error code from the shared CLI contract. An unknown code raises
            `KeyError`.
        message: Short explanation included in the JSON error.
        text: Full text-mode error; defaults to `Error: <message>`.
        diagnostic: Extra detail sent to stderr in JSON mode only.
        stdout: Stream for the JSON envelope; defaults to `sys.stdout`.
        stderr: Stream for text errors and diagnostics; defaults to `sys.stderr`.
    """
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    exit_code = _EXIT_CODES[code]

    if json_mode:
        payload = {"ok": False, "error": {"code": code, "message": message}}
        print(json.dumps(payload), file=output_stream)

        if diagnostic:
            print(diagnostic, file=error_stream)
    else:
        print(text or f"Error: {message}", file=error_stream)

    return exit_code
