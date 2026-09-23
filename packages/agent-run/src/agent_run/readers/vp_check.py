"""Read lint, typecheck, and format failures emitted by `vp check`."""

import re
from collections.abc import Sequence
from pathlib import Path

from agent_run.failures import Failure, FailureReport

# Error headings start with `x`. Warning headings start with `!` and are not failures.
_DIAGNOSTIC_HEADER = re.compile(r"^\s*x (?P<title>.+?)\s*$")

# Real vp output opens the location with either a hyphen or a box-drawing line.
_LOCATION = re.compile(
    r"^\s*,(?:-|─)\[(?P<path>.+):(?P<line>\d+):(?P<column>\d+)\]\s*$"
)

# Each file that needs formatting is listed with its timing, before the summary.
_FORMAT_FILE = re.compile(r"^(?P<path>.+?) \(\d+ms\)\s*$")

# Summary line printed once all files needing formatting are listed.
_FORMAT_SUMMARY = re.compile(r"^Found formatting issues in \d+ files?\b")

# Final line that confirms the run failed on formatting.
_FORMAT_ERROR = re.compile(r"^error: Formatting issues found\s*$")

# A code frame ends with a backtick and a horizontal line.
_FRAME_END = re.compile(r"^\s*`[-─]+\s*$")

# Source lines (`1 | ...`) and pointer lines (`: ^`) inside a code frame.
_FRAME_LINE = re.compile(r"^\s*(?:\d+\s*\||:)")


class VpCheckReader:
    """Read failures emitted by `vp check` and its common launchers."""

    def matches(self, argv: Sequence[str]) -> bool:
        """Return whether argv launches `vp check` through a supported command."""
        if not argv:
            return False

        executable = Path(argv[0]).name

        if executable == "vp":
            return len(argv) >= 2 and argv[1] == "check"

        if executable == "npx":
            return len(argv) >= 3 and tuple(argv[1:3]) == ("vp", "check")

        return (
            executable in {"npm", "pnpm"}
            and len(argv) >= 4
            and tuple(argv[1:4]) == ("exec", "vp", "check")
        )

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Keep the last code-frame line when the shared detail limit trims output."""
        code_frame = max(
            (index for index, line in enumerate(detail) if _FRAME_LINE.match(line)),
            default=None,
        )

        return code_frame, None

    def read(self, log_text: str) -> FailureReport | None:
        """Return the formatting failures, or every `x` error in the order vp printed them.

        Only the first error keeps its code frame. An error with no location line is
        still reported, with no path, line, or column.
        """
        lines = log_text.splitlines()

        format_report = _format_report(lines)

        if format_report is not None:
            return format_report

        starts = [
            (index, match)
            for index, line in enumerate(lines)
            if (match := _DIAGNOSTIC_HEADER.match(line)) is not None
        ]
        failures: list[Failure] = []

        for position, (start, header_match) in enumerate(starts):
            end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            section = lines[start:end]
            location = _diagnostic_location(section)
            path, line, column = location or (None, None, None)

            failures.append(
                Failure(
                    path=path,
                    line=line,
                    column=column,
                    title=header_match.group("title"),
                    detail=_code_frame(section) if not failures else (),
                )
            )

        if not failures:
            return None

        return FailureReport(
            recognised=True,
            first=failures[0],
            more=tuple(failures[1:]),
            hidden_count=0,
            truncated=False,
            tail=(),
        )

    def summarise(self, log_text: str) -> list[str] | None:
        """Keep the final passing format and lint lines from vp."""
        lines = [
            line.strip()
            for line in log_text.splitlines()
            if line.strip().startswith("pass: ")
        ]
        return lines[-2:] or None


def _format_report(lines: Sequence[str]) -> FailureReport | None:
    """Return one failure for each path in a formatting-failure summary."""
    summary_index = next(
        (index for index, line in enumerate(lines) if _FORMAT_SUMMARY.match(line)),
        None,
    )

    if summary_index is None or not any(
        _FORMAT_ERROR.match(line) for line in lines[summary_index + 1 :]
    ):
        return None

    paths = [
        match.group("path")
        for line in lines[:summary_index]
        if (match := _FORMAT_FILE.match(line)) is not None
    ]

    if not paths:
        return None

    failures = tuple(
        Failure(
            path=path,
            line=None,
            column=None,
            title="Formatting issues found",
            detail=(),
        )
        for path in paths
    )

    return FailureReport(
        recognised=True,
        first=failures[0],
        more=failures[1:],
        hidden_count=0,
        truncated=False,
        tail=(),
    )


def _diagnostic_location(lines: Sequence[str]) -> tuple[str, int, int] | None:
    """Return a diagnostic location, or None when its location line is absent."""
    for line in lines:
        match = _LOCATION.match(line)

        if match:
            return (
                match.group("path"),
                int(match.group("line")),
                int(match.group("column")),
            )

    return None


def _code_frame(lines: Sequence[str]) -> tuple[str, ...]:
    """Return source and pointer lines from a diagnostic's code frame."""
    location_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _LOCATION.match(line) is not None
        ),
        None,
    )

    frame_start = location_index if location_index is not None else 0

    detail: list[str] = []

    for line in lines[frame_start + 1 :]:
        if _FRAME_END.match(line):
            break

        if _FRAME_LINE.match(line):
            detail.append(line)

    return tuple(detail)
