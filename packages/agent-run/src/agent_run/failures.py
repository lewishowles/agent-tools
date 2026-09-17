"""Store bounded failure models and reader contracts."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

# Most lines of detail shown for the first failure before outer frames are dropped.
MAX_FAILURE_DETAIL_LINES = 20

# Most one-line entries listed after the first failure; the rest are counted.
MAX_ADDITIONAL_FAILURES = 20

# Log lines shown when no reader recognises the output.
MAX_TAIL_LINES = 15


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
    """Read one command's known failure format."""

    def matches(self, argv: Sequence[str]) -> bool:
        """Return whether this reader owns the command argument shape."""

    def read(self, log_text: str) -> FailureReport | None:
        """Return a report or `None` when the log has no known failures."""
