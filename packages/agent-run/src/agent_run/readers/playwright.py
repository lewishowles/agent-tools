"""Read Playwright test failures and final counts."""

import re
from collections.abc import Sequence
from pathlib import Path

from agent_run.failures import Failure, FailureReport

# Numbered failure headings appear after the progress output in Playwright's text reporters.
# The project label is left out when the project has no name, and Playwright pads a
# short heading to 100 columns with a line of dashes.
_FAILURE_HEADING = re.compile(
    r"^\s+\d+\) (?:\[[^]]+\] › )?(?P<path>.+?):(?P<line>\d+):(?P<column>\d+) › (?P<title>.+?)(?: ─+)?\s*$"
)

# Playwright's closing counts include lines such as "2 failed" and "16 passed (21.6s)".
_TOTAL = re.compile(r"^\s*\d+ (?:failed|flaky|passed|skipped)(?: \([^)]+\))?\s*$")

# Playwright marks the failing source line and its caret in the code frame.
_CODE_FRAME = re.compile(r"^\s*>\s*\d+\s*\|")
_CARET = re.compile(r"^\s*\|\s*\^")

# An error line such as "Error: expect(locator).toHaveClass(expected) failed" opens a failure's detail.
_ERROR = re.compile(r"^\s*[A-Za-z]*Error(?::|\s|$)")


class PlaywrightReader:
    """Read numbered failures from Playwright's list, line, and dot reporters."""

    def matches(self, argv: Sequence[str]) -> bool:
        """Recognise direct Playwright test commands and common package launchers."""
        if not argv:
            return False

        executable = Path(argv[0]).name
        arguments = list(argv[1:])

        if executable in {"npx", "bunx"}:
            return arguments[:2] == ["playwright", "test"]

        if executable == "npm":
            return arguments[:3] == ["exec", "playwright", "test"]

        if executable in {"pnpm", "yarn"}:
            if arguments and arguments[0] in {"exec", "dlx"}:
                arguments = arguments[1:]

            return arguments[:2] == ["playwright", "test"]

        return executable == "playwright" and arguments[:1] == ["test"]

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Keep the error message and failing code line when detail is shortened."""
        code_frame = max(
            (
                index
                for index, line in enumerate(detail)
                if _CODE_FRAME.match(line) or _CARET.match(line)
            ),
            default=None,
        )
        error_message = (
            0 if code_frame is not None and detail and _ERROR.match(detail[0]) else None
        )

        return code_frame, error_message

    def read(self, log_text: str) -> FailureReport | None:
        """Keep full detail for the first numbered failure and list later failures."""
        lines = log_text.splitlines()
        headings = [
            (index, match)
            for index, line in enumerate(lines)
            if (match := _FAILURE_HEADING.match(line)) is not None
        ]

        failures: list[Failure] = []

        for position, (start, match) in enumerate(headings):
            end = (
                headings[position + 1][0]
                if position + 1 < len(headings)
                else len(lines)
            )
            detail = _failure_detail(lines[start + 1 : end]) if position == 0 else ()

            failures.append(
                Failure(
                    path=match.group("path"),
                    line=int(match.group("line")),
                    column=int(match.group("column")),
                    title=match.group("title"),
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
        """Return Playwright's final failed, flaky, passed, and skipped totals."""
        totals = [line.strip() for line in log_text.splitlines() if _TOTAL.match(line)]

        return totals or None


def _failure_detail(lines: Sequence[str]) -> tuple[str, ...]:
    """Return a failure's lines up to Playwright's final counts, without blank lines at either end."""
    detail: list[str] = []

    for line in lines:
        if _TOTAL.match(line):
            break

        detail.append(line)

    while detail and not detail[0].strip():
        detail.pop(0)

    while detail and not detail[-1].strip():
        detail.pop()

    return tuple(detail)
