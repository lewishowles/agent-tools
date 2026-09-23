"""Read pytest failure sections and short summaries."""

import re
from collections.abc import Sequence
from pathlib import Path

from agent_run.failures import Failure, FailureReport

# Heading that starts the section holding each failure in full.
_FAILURES_HEADER = re.compile(r"^=+ FAILURES =+$")

# Heading that starts the one-line list of failed and errored tests.
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+$", re.IGNORECASE)

# Underscore rule naming one test, such as "___ test_name ___".
_FAILURE_BLOCK_HEADER = re.compile(r"^_+\s*(?P<title>.*?)\s*_+$")

# One summary entry, such as "FAILED tests/test_x.py::test_y - assert False".
_SUMMARY_LINE = re.compile(r"^(?P<kind>FAILED|ERROR)\s+(?P<summary>.+)$")

# A "path:line[:column][: message]" location printed by pytest.
_LOCATION = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+)(?::(?P<column>\d+))?(?::\s*(?P<title>.*))?$"
)

# A Python traceback frame, such as 'File "x.py", line 3, in run'.
_TRACEBACK_LOCATION = re.compile(
    r'^\s*File "(?P<path>.+)", line (?P<line>\d+)(?:, in .*)?$'
)

# A pytest traceback error line, which starts with the diagnostic marker `E`.
_PYTEST_ERROR_MESSAGE = re.compile(r"^\s*E(?:\s|$)")

# Captured output can contain text that looks like a source location.
_CAPTURED_OUTPUT_HEADER = re.compile(r"^-{4,}\s+Captured\b.*-{4,}$")
# Pytest's final passing line gives the count and duration, with a clock suffix after a minute.
_PASSING_SUMMARY = re.compile(r"^\d+ passed(?:, .*?)? in \S+(?: \(\d+:\d{2}:\d{2}\))?$")


class PytestReader:
    """Read failures emitted by pytest and its common launchers."""

    def matches(self, argv: Sequence[str]) -> bool:
        """Return whether argv launches pytest directly or through a wrapper."""
        if not argv:
            return False

        executable = Path(argv[0]).name

        if executable == "pytest":
            return True

        if executable.startswith("python"):
            return len(argv) >= 3 and tuple(argv[1:3]) == ("-m", "pytest")

        return (
            executable == "uv"
            and len(argv) >= 3
            and tuple(argv[1:3])
            == (
                "run",
                "pytest",
            )
        )

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Anchor on the `>` line that raised the failure and the last `E` line, which
        carries pytest's assertion message. The `>` anchor is limited to lines at or
        before the message so later output that starts with `>` is not taken for the
        code frame."""
        error_message = max(
            (
                index
                for index, line in enumerate(detail)
                if _PYTEST_ERROR_MESSAGE.match(line)
            ),
            default=None,
        )

        code_frame = max(
            (
                index
                for index, line in enumerate(detail)
                if line.lstrip().startswith(">")
                and (error_message is None or index <= error_message)
            ),
            default=None,
        )

        return code_frame, error_message

    def read(self, log_text: str) -> FailureReport | None:
        """Return pytest's first failure block and its short summary entries."""
        lines = log_text.splitlines()
        failures_start = _find_line(lines, _FAILURES_HEADER)

        if failures_start is None:
            return None

        summaries = _summary_entries(lines)
        block = _first_failure_block(lines, failures_start)

        if block is None:
            return None

        block_title, detail = block
        path, line, column, location_title = _failure_location(detail)
        summary_title = ""

        if summaries:
            _, separator, reason = summaries[0].partition(" - ")
            summary_title = reason if separator else ""

        title = summary_title or location_title or block_title
        first = Failure(
            path=path,
            line=line,
            column=column,
            title=title,
            detail=tuple(detail),
        )
        more = tuple(_summary_failure(summary) for summary in summaries[1:])

        return FailureReport(
            recognised=True,
            first=first,
            more=more,
            hidden_count=0,
            truncated=False,
            tail=(),
        )

    def summarise(self, log_text: str) -> list[str] | None:
        """Keep pytest's final passing totals and duration."""
        for line in reversed(log_text.splitlines()):
            clean = line.strip().strip("=").strip()
            if _PASSING_SUMMARY.match(clean):
                return [clean]

        return None


def _find_line(lines: Sequence[str], pattern: re.Pattern[str]) -> int | None:
    """Return the first line matching a section heading."""
    return next(
        (index for index, line in enumerate(lines) if pattern.match(line)),
        None,
    )


def _summary_entries(lines: Sequence[str]) -> list[str]:
    """Return pytest's failure summary entries in their printed order."""
    summary_start = _find_line(lines, _SUMMARY_HEADER)

    if summary_start is None:
        return []

    entries: list[str] = []

    for line in lines[summary_start + 1 :]:
        match = _SUMMARY_LINE.match(line)

        if match:
            entries.append(match.group("summary"))

    return entries


def _first_failure_block(
    lines: Sequence[str], failures_start: int
) -> tuple[str, list[str]] | None:
    """Return the first named block from pytest's failures section."""
    block_start = next(
        (
            index
            for index in range(failures_start + 1, len(lines))
            if _FAILURE_BLOCK_HEADER.match(lines[index])
        ),
        None,
    )

    if block_start is None:
        return None

    header_match = _FAILURE_BLOCK_HEADER.match(lines[block_start])
    if header_match is None:
        return None

    block_end = next(
        (
            index
            for index in range(block_start + 1, len(lines))
            if _FAILURE_BLOCK_HEADER.match(lines[index])
            or _SUMMARY_HEADER.match(lines[index])
        ),
        len(lines),
    )
    detail = list(lines[block_start + 1 : block_end])

    while detail and not detail[0].strip():
        detail.pop(0)

    while detail and not detail[-1].strip():
        detail.pop()

    return header_match.group("title").strip(), detail


def _failure_location(
    detail: Sequence[str],
) -> tuple[str | None, int | None, int | None, str | None]:
    """Return the last source location and diagnostic title in a block."""
    location: tuple[str | None, int | None, int | None, str | None] = (
        None,
        None,
        None,
        None,
    )

    for line in detail:
        if _CAPTURED_OUTPUT_HEADER.match(line.strip()):
            break

        stripped_line = line.strip()
        match = _LOCATION.match(stripped_line)

        if match:
            location = (
                match.group("path"),
                int(match.group("line")),
                int(match.group("column")) if match.group("column") else None,
                match.group("title") or None,
            )
            continue

        traceback_match = _TRACEBACK_LOCATION.match(line)

        if traceback_match:
            location = (
                traceback_match.group("path"),
                int(traceback_match.group("line")),
                None,
                None,
            )

    return location


def _summary_failure(summary: str) -> Failure:
    """Turn one short-summary line into a one-line failure record."""
    location_text, separator, reason = summary.partition(" - ")
    path = location_text.split("::", maxsplit=1)[0]
    title = reason if separator else summary

    return Failure(
        path=path,
        line=None,
        column=None,
        title=title,
        detail=(),
    )
