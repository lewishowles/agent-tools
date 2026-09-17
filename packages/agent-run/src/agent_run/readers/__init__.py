"""Choose a failure reader for a command and apply the output limits."""

import re
from collections.abc import Sequence

from agent_run.failures import (
    MAX_ADDITIONAL_FAILURES,
    MAX_FAILURE_DETAIL_LINES,
    MAX_TAIL_LINES,
    Failure,
    FailureReader,
    FailureReport,
)
from agent_run.readers.pytest import PytestReader
from agent_run.readers.ruff import RuffReader

# Readers are tried in this order, and the first that matches a command reads its log.
FAILURE_READERS: tuple[FailureReader, ...] = (PytestReader(), RuffReader())


def read_failure_report(argv: Sequence[str], log_text: str) -> FailureReport:
    """Return the first failure in a command log, within the output limits.

    The first reader that matches the command reads the log. When no reader
    matches, or the reader finds no failure, the report holds the last log lines
    and is marked as not recognised.
    """
    reader = next(
        (candidate for candidate in FAILURE_READERS if candidate.matches(argv)),
        None,
    )

    if reader is None:
        return _fallback_report(log_text)

    report = reader.read(log_text)

    if report is None or report.first is None:
        return _fallback_report(log_text)

    return _bound_report(report)


def _fallback_report(log_text: str) -> FailureReport:
    """Keep the end of an unrecognised log as evidence of the failure."""
    return FailureReport(
        recognised=False,
        first=None,
        more=(),
        hidden_count=0,
        truncated=False,
        tail=tuple(log_text.splitlines()[-MAX_TAIL_LINES:]),
    )


def _bound_report(report: FailureReport) -> FailureReport:
    """Apply the shared detail and additional-failure limits."""
    first = report.first
    truncated = report.truncated

    if first is not None:
        detail, detail_truncated = _bound_detail(first.detail)
        first = Failure(
            path=first.path,
            line=first.line,
            column=first.column,
            title=first.title,
            detail=detail,
        )
        truncated = truncated or detail_truncated

    more = report.more[:MAX_ADDITIONAL_FAILURES]
    hidden_count = report.hidden_count + max(
        0, len(report.more) - MAX_ADDITIONAL_FAILURES
    )
    truncated = truncated or hidden_count > report.hidden_count

    return FailureReport(
        recognised=report.recognised,
        first=first,
        more=more,
        hidden_count=hidden_count,
        truncated=truncated,
        tail=tuple(report.tail[-MAX_TAIL_LINES:]),
    )


def _bound_detail(detail: Sequence[str]) -> tuple[tuple[str, ...], bool]:
    """Trim long failure detail, keeping the error message and the line that raised it."""
    detail_lines = tuple(detail)

    if len(detail_lines) <= MAX_FAILURE_DETAIL_LINES:
        return detail_lines, False

    code_frame, error_message = _detail_anchors(detail_lines)

    last_anchor = error_message if error_message is not None else code_frame

    if last_anchor is None:
        return detail_lines[-MAX_FAILURE_DETAIL_LINES:], True

    window_start = max(0, last_anchor - MAX_FAILURE_DETAIL_LINES + 1)
    detail_window = detail_lines[window_start : last_anchor + 1]

    if code_frame is not None and code_frame < window_start:
        return (
            (
                detail_lines[code_frame],
                *detail_window[-(MAX_FAILURE_DETAIL_LINES - 1) :],
            ),
            True,
        )

    return detail_window, True


def _detail_anchors(
    detail: Sequence[str],
) -> tuple[int | None, int | None]:
    """Return the nearest code-frame and final error-message indexes."""
    error_message = next(
        (
            index
            for index in range(len(detail) - 1, -1, -1)
            if re.match(r"^\s*E(?:\s|$)", detail[index])
        ),
        None,
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
