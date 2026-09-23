"""Tests for selecting readers and bounding failure reports."""

from collections.abc import Sequence

import pytest
from agent_run.failures import Failure, FailureReport, summarise_output
from agent_run.readers import read_failure_report


def test_read_failure_report_selects_xcodebuild_reader() -> None:
    """An xcrun-launched build uses Xcode diagnostics instead of the log tail."""
    report = read_failure_report(
        ["xcrun", "xcodebuild", "build"],
        "File.swift:3:2: error: broken\n** BUILD FAILED **\n",
    )

    assert report.recognised is True
    assert report.first is not None
    assert report.first.path == "File.swift"
    assert report.first.title == "broken"


class _StubReader:
    """Return a known report so reader selection can be tested in isolation."""

    def __init__(
        self,
        should_match: bool,
        report: FailureReport | None,
        anchors: tuple[int | None, int | None] = (None, None),
    ) -> None:
        """Store the match result, report, and detail anchors returned by this stub."""
        self.should_match = should_match
        self.report = report
        self.anchors = anchors
        self.read_calls = 0

    def matches(self, argv: Sequence[str]) -> bool:
        """Return the configured match result."""
        return self.should_match

    def read(self, log_text: str) -> FailureReport | None:
        """Return the configured report and record that parsing was attempted."""
        self.read_calls += 1
        return self.report

    def detail_anchors(self, detail: Sequence[str]) -> tuple[int | None, int | None]:
        """Return the configured anchors so the trimming window can be tested without a real reader."""
        return self.anchors


def _failure(detail: tuple[str, ...] = ()) -> Failure:
    """Create a small failure record for report-limit tests."""
    return Failure(
        path="tests/test_example.py",
        line=4,
        column=None,
        title="AssertionError",
        detail=detail,
    )


@pytest.mark.parametrize(
    ("log_text", "expected"),
    [
        (
            (
                "============================= test session starts =============================\n"
                "collected 176 items\n\n"
                "====================== 176 passed in 0.42s ======================\n"
            ),
            [
                "============================= test session starts =============================",
                "collected 176 items",
                "====================== 176 passed in 0.42s ======================",
            ],
        ),
        ("All checks passed!\n", ["All checks passed!"]),
        ("first\n\nsecond\n\n\n", ["first", "second"]),
        ("\x1b[32mpassed\x1b[0m\n", ["passed"]),
        ("", ["No output."]),
    ],
)
def test_summarise_output_returns_bounded_clean_lines(
    log_text: str, expected: list[str]
) -> None:
    """Summaries keep useful test and lint output without terminal colour codes."""
    assert summarise_output(log_text) == expected


def test_summarise_output_keeps_only_the_last_eight_non_blank_lines() -> None:
    """Long command output is bounded to its final eight lines."""
    log_text = "\n".join(f"line {index}" for index in range(10))

    assert summarise_output(log_text) == [f"line {index}" for index in range(2, 10)]


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
    """Long detail keeps the lines the reader anchors."""
    detail = tuple(f"  outer frame {index}" for index in range(25)) + (
        ">       assert actual == expected",
        "E       AssertionError: values differ",
    )
    reader = _StubReader(
        True,
        FailureReport(True, _failure(detail), (), 0, False, ()),
        anchors=(25, 26),
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
