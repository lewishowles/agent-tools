"""Choose a failure reader for a command and apply the output limits."""

from collections.abc import Sequence

from agent_run.failures import (
    MAX_ADDITIONAL_FAILURES,
    MAX_FAILURE_DETAIL_LINES,
    MAX_TAIL_LINES,
    Failure,
    FailureReader,
    FailureReport,
    summarise_output,
)
from agent_run.readers.pytest import PytestReader
from agent_run.readers.ruff import RuffReader
from agent_run.readers.vitest import VitestReader
from agent_run.readers.vp_check import VpCheckReader
from agent_run.readers.xcodebuild import XcodebuildReader

# Readers are tried in this order, and the first that matches a command reads its log.
FAILURE_READERS: tuple[FailureReader, ...] = (
    PytestReader(),
    RuffReader(),
    VitestReader(),
    VpCheckReader(),
    XcodebuildReader(),
)


def summarise_success(argv: Sequence[str], log_text: str) -> list[str]:
    """Use a matching reader's success lines, or keep the bounded log tail."""
    reader = next(
        (candidate for candidate in FAILURE_READERS if candidate.matches(argv)),
        None,
    )
    summary = reader.summarise(log_text) if reader is not None else None
    return summary or summarise_output(log_text)


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

    return _bound_report(report, reader)


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


def _bound_report(report: FailureReport, reader: FailureReader) -> FailureReport:
    """Apply the shared detail and additional-failure limits."""
    first = report.first
    truncated = report.truncated

    if first is not None:
        detail, detail_truncated = _bound_detail(first.detail, reader)
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


def _bound_detail(
    detail: Sequence[str], reader: FailureReader
) -> tuple[tuple[str, ...], bool]:
    """Trim long failure detail, keeping the error message and the line that raised it."""
    detail_lines = tuple(detail)

    if len(detail_lines) <= MAX_FAILURE_DETAIL_LINES:
        return detail_lines, False

    code_frame, error_message = reader.detail_anchors(detail_lines)

    # A long diff can push the code frame far below the error message, so keep the message
    # and as much of the frame as the limit allows rather than the tail.
    if (
        error_message is not None
        and code_frame is not None
        and code_frame > error_message
    ):
        code_frame_start = max(
            error_message + 1,
            code_frame - MAX_FAILURE_DETAIL_LINES + 2,
        )
        code_frame_detail = detail_lines[code_frame_start : code_frame + 1]
        return (detail_lines[error_message], *code_frame_detail), True

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
