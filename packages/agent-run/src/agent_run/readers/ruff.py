"""Read Ruff diagnostics and source code excerpts.

Output from `ruff check --output-format concise` is not recognised, so it falls back to the last log lines.
"""

import re
from collections.abc import Sequence
from pathlib import Path

from agent_run.failures import Failure, FailureReport

# A Ruff diagnostic heading, such as "F401 [*] `os` imported but unused".
_DIAGNOSTIC_HEADER = re.compile(
    r"^(?P<title>(?P<code>[A-Z][A-Z0-9]*\d{3})(?:\s+\[\*\])?\s+.+)$"
)

# The source location printed above each Ruff code excerpt.
_LOCATION = re.compile(r"^\s*-->\s+(?P<path>.+):(?P<line>\d+):(?P<column>\d+)\s*$")


class RuffReader:
    """Read diagnostics emitted by Ruff and its common launchers."""

    def matches(self, argv: Sequence[str]) -> bool:
        """Return whether argv runs `ruff check`, directly or through python -m or uv run."""
        if not argv:
            return False

        executable = Path(argv[0]).name

        if executable == "ruff":
            return len(argv) >= 2 and argv[1] == "check"

        if executable.startswith("python"):
            return len(argv) >= 4 and tuple(argv[1:4]) == (
                "-m",
                "ruff",
                "check",
            )

        return (
            executable == "uv"
            and len(argv) >= 4
            and tuple(argv[1:4])
            == (
                "run",
                "ruff",
                "check",
            )
        )

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Ruff details are already short, so nothing is anchored."""
        return None, None

    def read(self, log_text: str) -> FailureReport | None:
        """Return Ruff's first diagnostic and one-line entries for later diagnostics."""
        lines = log_text.splitlines()
        starts: list[tuple[int, re.Match[str]]] = []

        for index, line in enumerate(lines):
            header_match = _DIAGNOSTIC_HEADER.match(line)

            if header_match is not None:
                starts.append((index, header_match))

        first: Failure | None = None
        more: list[Failure] = []

        for position, (start, header_match) in enumerate(starts):
            end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            section = lines[start:end]

            location = _diagnostic_location(section)

            if location is None:
                continue

            path, line, column = location
            failure = Failure(
                path=path,
                line=line,
                column=column,
                title=header_match.group("title"),
                detail=_code_excerpt(section) if first is None else (),
            )

            if first is None:
                first = failure
            else:
                more.append(failure)

        if first is None:
            return None

        return FailureReport(
            recognised=True,
            first=first,
            more=tuple(more),
            hidden_count=0,
            truncated=False,
            tail=(),
        )


def _diagnostic_location(
    lines: Sequence[str],
) -> tuple[str, int, int] | None:
    """Return the source location from a Ruff diagnostic section."""
    for line in lines:
        match = _LOCATION.match(line)

        if match:
            return (
                match.group("path"),
                int(match.group("line")),
                int(match.group("column")),
            )

    return None


def _code_excerpt(lines: Sequence[str]) -> tuple[str, ...]:
    """Return the source and pointer lines from the first Ruff code frame."""
    frame_start = next(
        (index for index, line in enumerate(lines) if line.strip() == "|"),
        None,
    )

    if frame_start is None:
        return ()

    frame_end = next(
        (
            index
            for index in range(frame_start + 1, len(lines))
            if lines[index].strip() == "|" or lines[index].lstrip().startswith("help:")
        ),
        None,
    )

    if frame_end is None:
        return ()

    return tuple(line for line in lines[frame_start + 1 : frame_end] if line.strip())
