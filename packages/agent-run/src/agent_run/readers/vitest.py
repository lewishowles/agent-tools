"""Read Vitest failure sections and source locations."""

import re
from collections.abc import Sequence
from pathlib import Path

from agent_run.failures import Failure, FailureReport

# A Vitest failure heading, such as " FAIL  tests/example.test.js > suite > test".
_FAILURE_HEADER = re.compile(r"^ FAIL  (?P<heading>.+)$")

# A file-level Vitest heading repeats its path in square brackets.
_FILE_LEVEL_HEADER = re.compile(r"^(?P<path>.+) \[ (?P=path) \]$")

# The source location printed below a Vitest error message and diff. Thrown errors
# can prefix the path with a stack-frame label, such as `Object.<anonymous>`.
_LOCATION = re.compile(
    r"^\s*❯\s+(?:\S+\s+)?(?P<path>.+):(?P<line>\d+):(?P<column>\d+)\s*$"
)

# A Vitest error message starts the first failure detail after leading blanks are removed.
_VITEST_ERROR_MESSAGE = re.compile(r"^(?:[A-Za-z]+Error|Error)(?::|\s|$)")

# Vitest source gutters and caret lines identify the code frame after its error message.
_VITEST_GUTTER = re.compile(r"^\s+\d+\|")
_VITEST_CARET = re.compile(r"^\s*\|\s*\^")

# Separators and progress counters do not belong to a failure's detail.
_SEPARATOR = re.compile(r"^\s*⎯+")
_COUNTER = re.compile(r"^\s*\[\d+/\d+\]\s*$")
# Vitest prints passing file and test totals near the end of a run.
_PASSING_TOTAL = re.compile(r"^(?P<heading>Test Files|Tests)\s+\d+ passed\b")


class VitestReader:
    """Read failures emitted by Vitest and its common launchers."""

    def matches(self, argv: Sequence[str]) -> bool:
        """Return whether argv launches Vitest through a supported command."""
        if not argv:
            return False

        executable = Path(argv[0]).name

        if executable == "vitest":
            return len(argv) == 1 or argv[1] in {"run", "--run"}

        if executable == "npx":
            return len(argv) >= 2 and argv[1] == "vitest"

        if executable in {"npm", "pnpm"}:
            return len(argv) >= 3 and tuple(argv[1:3]) == ("exec", "vitest")

        return (
            executable == "vp"
            and len(argv) >= 2
            and argv[1] == "test"
            and "--watch" not in argv[2:]
        )

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Anchor on the last gutter or caret line of the code frame, and on line 0 when it opens with an error class
        and a code frame follows."""
        code_frame = max(
            (
                index
                for index, line in enumerate(detail)
                if _VITEST_GUTTER.match(line) or _VITEST_CARET.match(line)
            ),
            default=None,
        )
        error_message = (
            0
            if (
                code_frame is not None
                and detail
                and _VITEST_ERROR_MESSAGE.match(detail[0])
            )
            else None
        )

        return code_frame, error_message

    def read(self, log_text: str) -> FailureReport | None:
        """Return Vitest's first detailed failure and later failure entries."""
        lines = log_text.splitlines()
        starts = [
            (index, match)
            for index, line in enumerate(lines)
            if (match := _FAILURE_HEADER.match(line)) is not None
        ]

        failures: list[Failure] = []

        for position, (start, header_match) in enumerate(starts):
            end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            section = lines[start + 1 : end]
            heading = header_match.group("heading").strip()
            file_level_match = _FILE_LEVEL_HEADER.fullmatch(heading)

            if file_level_match:
                heading_path = file_level_match.group("path")
                title = heading_path
            else:
                heading_parts = heading.split(" > ")
                heading_path = heading_parts[0]
                title = " > ".join(heading_parts[1:]) or heading_path
            location = _failure_location(section)

            if location is None:
                path, line, column = heading_path, None, None
            else:
                path, line, column = location

            detail = _failure_detail(section) if position == 0 else ()
            failures.append(
                Failure(
                    path=path,
                    line=line,
                    column=column,
                    title=title,
                    detail=detail,
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
        """Keep Vitest's final file and test totals."""
        totals: dict[str, str] = {}
        for line in log_text.splitlines():
            clean = line.strip()
            match = _PASSING_TOTAL.match(clean)
            if match:
                totals[match.group("heading")] = clean

        return [
            totals[heading] for heading in ("Test Files", "Tests") if heading in totals
        ] or None


def _failure_location(
    lines: Sequence[str],
) -> tuple[str, int, int] | None:
    """Return the source location from a Vitest failure section."""
    for line in lines:
        match = _LOCATION.match(line)

        if match:
            return (
                match.group("path"),
                int(match.group("line")),
                int(match.group("column")),
            )

    return None


def _failure_detail(lines: Sequence[str]) -> tuple[str, ...]:
    """Return the first failure's error message, diff, and code frame."""
    detail: list[str] = []

    for line in lines:
        if _SEPARATOR.match(line) or _COUNTER.match(line):
            break

        if _LOCATION.match(line):
            continue

        detail.append(line)

    while detail and not detail[0].strip():
        detail.pop(0)

    while detail and not detail[-1].strip():
        detail.pop()

    return tuple(detail)
