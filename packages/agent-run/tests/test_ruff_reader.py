"""Tests for the Ruff failure reader."""

from pathlib import Path

import pytest
from agent_run.readers import read_failure_report
from agent_run.readers.ruff import RuffReader

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "argv",
    [
        ["ruff", "check", "src"],
        ["/opt/venv/bin/ruff", "check"],
        ["python", "-m", "ruff", "check", "src"],
        ["python3", "-m", "ruff", "check"],
        ["uv", "run", "ruff", "check", "src"],
    ],
)
def test_ruff_reader_matches_supported_launchers(argv: list[str]) -> None:
    """The reader recognises Ruff check through supported launchers."""
    assert RuffReader().matches(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["ruff", "format", "src"],
        ["python", "-m", "ruff", "format"],
        ["uv", "run", "ruff", "format"],
        ["python", "script.py"],
        ["scripts/run-lint"],
    ],
)
def test_ruff_reader_ignores_other_launchers(argv: list[str]) -> None:
    """The reader leaves Ruff format and other commands to the fallback."""
    assert RuffReader().matches(argv) is False


def test_ruff_reader_reads_one_diagnostic_in_full() -> None:
    """The first Ruff diagnostic includes its location and code excerpt."""
    log_text = (FIXTURES / "ruff-single.txt").read_text(encoding="utf-8")

    report = RuffReader().read(log_text)

    assert report is not None
    assert report.recognised is True
    assert report.first is not None
    assert report.first.path == "src/example.py"
    assert report.first.line == 1
    assert report.first.column == 8
    assert report.first.title == "F401 [*] `os` imported but unused"
    assert report.first.detail == (
        "1 | import os",
        "  |        ^^",
    )
    assert report.more == ()


def test_ruff_reader_lists_later_diagnostics_without_detail() -> None:
    """Later Ruff diagnostics keep their location and title but not their code excerpt."""
    log_text = (FIXTURES / "ruff-several.txt").read_text(encoding="utf-8")

    report = RuffReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/example.py"
    assert report.first.title == "F401 [*] `os` imported but unused"
    assert len(report.more) == 1
    assert report.more[0].path == "src/example.py"
    assert report.more[0].line == 2
    assert report.more[0].column == 8
    assert report.more[0].title == "F401 [*] `sys` imported but unused"
    assert report.more[0].detail == ()


def test_ruff_reader_stops_code_excerpt_before_help_on_the_last_line() -> None:
    """A last-line diagnostic ends its excerpt at Ruff's help line."""
    log_text = (FIXTURES / "ruff-last-line.txt").read_text(encoding="utf-8")

    report = RuffReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/example.py"
    assert report.first.line == 3
    assert report.first.column == 8
    assert report.first.detail == (
        "3 | import os",
        "  |        ^^",
    )


def test_ruff_reader_returns_none_for_clean_output() -> None:
    """A clean Ruff run has no failure report to return."""
    log_text = (FIXTURES / "ruff-clean.txt").read_text(encoding="utf-8")

    assert RuffReader().read(log_text) is None


def test_read_failure_report_falls_back_for_unrecognised_ruff_output() -> None:
    """Ruff output the reader cannot parse falls back to the last log lines."""
    log_text = (FIXTURES / "ruff-fallback.txt").read_text(encoding="utf-8")

    report = read_failure_report(["ruff", "check"], log_text)

    assert report.recognised is False
    assert report.first is None
    assert report.tail == tuple(log_text.splitlines())
