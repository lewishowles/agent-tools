"""Store bounded failure models, success summaries, and reader contracts."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

# Most lines of detail shown for the first failure before outer frames are dropped.
MAX_FAILURE_DETAIL_LINES = 20

# Most one-line entries listed after the first failure; the rest are counted.
MAX_ADDITIONAL_FAILURES = 20

# Log lines shown when no reader recognises the output.
MAX_TAIL_LINES = 15

# Log lines shown after a successful run.
MAX_SUMMARY_LINES = 8

# Most characters shown from a fallback log line, including its ellipsis.
MAX_FALLBACK_LINE_LENGTH = 300

# Terminal colour escape sequences removed from captured output.
ANSI_SGR_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def summarise_output(log_text: str) -> list[str]:
    """Return the last eight non-blank log lines, without terminal colour codes.

    Agents use these lines to confirm what a successful command did, such as
    how many tests ran. Empty or whitespace-only output returns
    ["No output."].
    """
    clean = ANSI_SGR_PATTERN.sub("", log_text).strip()
    if not clean:
        return ["No output."]

    lines = [line for line in clean.splitlines() if line.strip()]
    return [shorten_fallback_line(line) for line in lines[-MAX_SUMMARY_LINES:]]


def shorten_fallback_line(line: str) -> str:
    """Cut a long fallback log line to the length limit, ending it with an ellipsis."""
    if len(line) <= MAX_FALLBACK_LINE_LENGTH:
        return line

    return f"{line[: MAX_FALLBACK_LINE_LENGTH - 1]}…"


@dataclass(frozen=True)
class Failure:
    """Describe one failure and its optional source location.

    Attributes:
        path: Source path reported by the command, when available.
        line: One-based source line, when available.
        column: One-based source column, when available.
        title: Short description of the failure.
        detail: Detail lines belonging to the first failure.
    """

    path: str | None
    line: int | None
    column: int | None
    title: str
    detail: tuple[str, ...]


@dataclass(frozen=True)
class FailureReport:
    """Hold the first failure, bounded additional failures, or a log tail.

    Attributes:
        recognised: Whether a reader identified actionable failure structure.
        first: The first actionable failure, when one was identified.
        more: Additional failures kept after the first failure.
        hidden_count: Additional failures omitted by the output limit.
        truncated: Whether a detail or additional-failure limit was applied.
        tail: The final log lines used when no reader recognised the output.
    """

    recognised: bool
    first: Failure | None
    more: tuple[Failure, ...]
    hidden_count: int
    truncated: bool
    tail: tuple[str, ...]


class FailureReader(Protocol):
    """Read one command's known failure and success formats."""

    def matches(self, argv: Sequence[str]) -> bool:
        """Return whether this reader owns the command argument shape."""

    def read(self, log_text: str) -> FailureReport | None:
        """Return a report or `None` when the log has no known failures."""

    def summarise(self, log_text: str) -> list[str] | None:
        """Return useful success lines, or `None` when none are recognised."""

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Return the code-frame and error-message line indexes to keep when the detail
        is trimmed. Either is `None` when the reader cannot find it."""
