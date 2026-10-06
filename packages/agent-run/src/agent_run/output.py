"""Write agent-run results as text or as the shared JSON envelope."""

import json
import sys
from collections.abc import Sequence
from typing import TextIO

from cli_style import (
    empty_state,
    quote_block,
    row_group,
    status,
)

# Exit status for each error code in the shared CLI contract.
_EXIT_CODES = {
    "usage": 2,
    "not-found": 1,
    "check-failed": 1,
    "busy": 1,
    "manual": 1,
    "environment": 3,
    "uninitialised": 3,
    "internal": 3,
}

# The options every cli-style call receives. cli-style picks colour from the
# terminal, and prints plain text when its executable is not installed instead
# of failing the command. cli-style cannot see the terminal width itself, so
# output that wraps takes agent-run's width separately. Tests
# replace these options with plain output at a fixed width so their expected
# text never changes.
_RENDER_OPTIONS: dict[str, object] = {"raise_on_missing": False}


def render_empty_state(*, title: str, detail: str = "") -> str:
    """Return the message shown when a list or runs command has nothing to show."""
    return empty_state(
        title=title,
        detail=detail,
        **_RENDER_OPTIONS,
    )


def render_row_group(
    rows: Sequence[dict[str, object]], *, width: int | None = None
) -> str:
    """Return label and value rows with the values lined up in one column.

    Args:
        rows: The label and value rows, in display order.
        width: The terminal width in columns, or None to use cli-style's
            fallback of the COLUMNS variable or 80 columns.
    """
    return row_group(
        rows=list(rows),
        **_render_options(width),
    )


def render_quote_block(
    *, lines: Sequence[str], title: str, width: int | None = None
) -> str:
    """Return command output lines under a heading, each marked as a quote.

    Args:
        lines: The output lines, already stripped of colour codes.
        title: The heading shown above the lines.
        width: The terminal width in columns, or None to use cli-style's
            fallback of the COLUMNS variable or 80 columns. Lines longer than
            the width wrap inside the quote.
    """
    return quote_block(
        lines=list(lines),
        title=title,
        **_render_options(width),
    )


def _render_options(width: int | None) -> dict[str, object]:
    """Return the shared render options, with the terminal width when known.

    Leaving the width out when it is None keeps cli-style's own fallback, and
    keeps the fixed width that tests set.
    """
    if width is None:
        return _RENDER_OPTIONS

    return _RENDER_OPTIONS | {"width": width}


def render_success_status(*, label: str) -> str:
    """Return the success marker and label for a completed command."""
    return status(
        type="success",
        label=label,
        **_RENDER_OPTIONS,
    )


def render_error_status(*, label: str) -> str:
    """Return the error marker and label for a command that failed or timed out."""
    return status(
        type="error",
        label=label,
        **_RENDER_OPTIONS,
    )


def render_warning_status(*, label: str) -> str:
    """Return the warning marker and label for an interrupted command."""
    return status(
        type="warning",
        label=label,
        **_RENDER_OPTIONS,
    )


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
    data: object | None = None,
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
        data: Optional structured details included in the JSON error.
        text: Full text-mode error; defaults to `Error: <message>`.
        diagnostic: Extra detail sent to stderr in JSON mode only.
        stdout: Stream for the JSON envelope; defaults to `sys.stdout`.
        stderr: Stream for text errors and diagnostics; defaults to `sys.stderr`.
    """
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    exit_code = _EXIT_CODES[code]

    if json_mode:
        error = {"code": code, "message": message}

        if data is not None:
            error["data"] = data

        payload = {"ok": False, "error": error}
        print(json.dumps(payload), file=output_stream)

        if diagnostic:
            print(diagnostic, file=error_stream)
    else:
        print(text or f"Error: {message}", file=error_stream)

    return exit_code
