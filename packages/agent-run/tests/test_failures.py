"""Tests for selecting readers and bounding failure reports."""

from collections.abc import Sequence

from agent_run.failures import Failure, FailureReport
from agent_run.readers import read_failure_report


class _StubReader:
    """Return a known report so reader selection can be tested in isolation."""

    def __init__(self, should_match: bool, report: FailureReport | None) -> None:
        """Store the matching result and report returned by this stub."""
        self.should_match = should_match
        self.report = report
        self.read_calls = 0

    def matches(self, argv: Sequence[str]) -> bool:
        """Return the configured match result."""
        return self.should_match

    def read(self, log_text: str) -> FailureReport | None:
        """Return the configured report and record that parsing was attempted."""
        self.read_calls += 1
        return self.report


def _failure(detail: tuple[str, ...] = ()) -> Failure:
    """Create a small failure record for report-limit tests."""
    return Failure(
        path="tests/test_example.py",
        line=4,
        column=None,
        title="AssertionError",
        detail=detail,
    )


def test_read_failure_report_uses_the_first_matching_reader(monkeypatch) -> None:
    """Only the first matching reader parses a command log."""
    first_reader = _StubReader(
        True,
        FailureReport(True, _failure(), (), 0, False, ()),
    )
    second_reader = _StubReader(
        True,
        FailureReport(True, _failure(), (), 0, False, ()),
    )
    monkeypatch.setattr(
        "agent_run.readers.FAILURE_READERS",
        (first_reader, second_reader),
    )

    report = read_failure_report(["custom-check"], "failure")

    assert report.recognised is True
    assert first_reader.read_calls == 1
    assert second_reader.read_calls == 0


def test_read_failure_report_falls_back_to_the_last_15_lines(monkeypatch) -> None:
    """An unknown command keeps only the final fifteen log lines."""
    monkeypatch.setattr("agent_run.readers.FAILURE_READERS", ())
    log_text = "\n".join(f"line {index}" for index in range(20))

    report = read_failure_report(["custom-check"], log_text)

    assert report.recognised is False
    assert report.first is None
    assert report.more == ()
    assert report.tail == tuple(f"line {index}" for index in range(5, 20))


def test_read_failure_report_falls_back_when_a_reader_finds_no_failure(
    monkeypatch,
) -> None:
    """A matching reader that cannot parse the log does not hide its tail."""
    reader = _StubReader(False, None)
    monkeypatch.setattr("agent_run.readers.FAILURE_READERS", (reader,))
    log_text = "parser failed\nno failure section"

    report = read_failure_report(["custom-check"], log_text)

    assert report.recognised is False
    assert report.tail == ("parser failed", "no failure section")


def test_read_failure_report_trims_detail_around_the_code_frame_and_message(
    monkeypatch,
) -> None:
    """Long detail keeps pytest's code frame and error message."""
    detail = tuple(f"  outer frame {index}" for index in range(25)) + (
        ">       assert actual == expected",
        "E       AssertionError: values differ",
    )
    reader = _StubReader(
        True,
        FailureReport(True, _failure(detail), (), 0, False, ()),
    )
    monkeypatch.setattr("agent_run.readers.FAILURE_READERS", (reader,))

    report = read_failure_report(["custom-check"], "failure")

    assert report.first is not None
    assert len(report.first.detail) == 20
    assert ">       assert actual == expected" in report.first.detail
    assert "E       AssertionError: values differ" in report.first.detail
    assert "  outer frame 0" not in report.first.detail
    assert report.truncated is True


def test_read_failure_report_counts_hidden_additional_failures(monkeypatch) -> None:
    """Only twenty additional failures are returned and the rest are counted."""
    more = tuple(
        Failure(
            path=f"tests/test_{index}.py",
            line=None,
            column=None,
            title=f"FAILED test_{index}",
            detail=(),
        )
        for index in range(25)
    )
    reader = _StubReader(
        True,
        FailureReport(True, _failure(), more, 2, False, ()),
    )
    monkeypatch.setattr("agent_run.readers.FAILURE_READERS", (reader,))

    report = read_failure_report(["custom-check"], "failure")

    assert len(report.more) == 20
    assert report.hidden_count == 7
    assert report.truncated is True
