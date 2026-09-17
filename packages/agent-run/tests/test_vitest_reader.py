"""Tests for the Vitest failure reader."""

from pathlib import Path

import pytest
from agent_run.readers import read_failure_report
from agent_run.readers.vitest import VitestReader

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "argv",
    [
        ["vitest"],
        ["vitest", "run", "tests"],
        ["vitest", "--run"],
        ["/opt/venv/bin/vitest", "run"],
        ["npx", "vitest"],
        ["pnpm", "exec", "vitest", "run"],
        ["npm", "exec", "vitest"],
        ["vp", "test"],
    ],
)
def test_vitest_reader_matches_supported_launchers(argv: list[str]) -> None:
    """The reader recognises Vitest through supported launchers."""
    assert VitestReader().matches(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["vitest", "watch"],
        ["npx", "other-command"],
        ["pnpm", "vitest"],
        ["npm", "vitest"],
        ["vp", "test", "--watch"],
        ["vp", "check"],
        ["vp", "lint"],
        ["scripts/run-tests"],
    ],
)
def test_vitest_reader_ignores_other_launchers(argv: list[str]) -> None:
    """The reader leaves other Vitest subcommands and wrappers to the fallback."""
    assert VitestReader().matches(argv) is False


def test_vitest_reader_reads_one_failure_in_full() -> None:
    """The first Vitest failure includes its location, diff, and code frame."""
    log_text = (FIXTURES / "vitest-single.txt").read_text(encoding="utf-8")

    report = VitestReader().read(log_text)

    assert report is not None
    assert report.recognised is True
    assert report.first is not None
    assert report.first.path == "lib/number/round.test.js"
    assert report.first.line == 9
    assert report.first.column == 25
    assert (
        report.first.title
        == "round > should round a number to the specified number of decimal places"
    )
    assert report.first.detail == (
        "AssertionError: expected 2 to be 1 // Object.is equality",
        "",
        "- Expected",
        "+ Received",
        "",
        "- 1",
        "+ 2",
        "",
        "      7|   expect(round(1.2355, 2)).toBe(1.24);",
        "      8|   expect(round(1.2345, 0)).toBe(1);",
        "      9|   expect(round(1.5, 0)).toBe(1);",
        "       |                         ^",
        "     10|  });",
        "     11|",
    )
    assert report.more == ()


def test_vitest_reader_lists_later_failures_without_detail() -> None:
    """Later Vitest failures keep their location and title but not their detail."""
    log_text = (FIXTURES / "vitest-several.txt").read_text(encoding="utf-8")

    report = VitestReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert (
        report.first.title
        == "round > should round a number to the specified number of decimal places"
    )
    assert len(report.more) == 1
    assert report.more[0].path == "lib/number/round.test.js"
    assert report.more[0].line == 27
    assert report.more[0].column == 28
    assert report.more[0].title == "round > should handle negative decimal places"
    assert report.more[0].detail == ()


def test_vitest_reader_keeps_nested_suite_names_and_multiline_diff() -> None:
    """Nested suites preserve the test name and all lines in the first diff."""
    log_text = (FIXTURES / "vitest-nested.txt").read_text(encoding="utf-8")

    report = VitestReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/components/content/data-table/data-table.test.js"
    assert report.first.line == 121
    assert report.first.column == 70
    assert (
        report.first.title
        == "data-table > Render > Shows a custom empty message and hides the toolbar when it has nothing to show"
    )
    assert "- true" in report.first.detail
    assert "+ false" in report.first.detail
    assert "121|    expect(wrapper.find" in "\n".join(report.first.detail)
    assert len(report.more) == 3
    assert report.more[0].title == (
        "data-table > Render > renders the actions slot in the injected column"
    )
    assert report.more[0].line == 179
    assert report.more[2].title == (
        "data-table > Server mode > renders the supplied page without applying local search, sort, or pagination"
    )


def test_vitest_reader_bounds_long_first_detail() -> None:
    """A long first Vitest failure keeps its error message and code frame."""
    log_text = (FIXTURES / "vitest-long-first.txt").read_text(encoding="utf-8")

    report = read_failure_report(["vitest", "run"], log_text)

    assert report is not None
    assert report.first is not None
    assert len(report.first.detail) == 20
    assert report.first.detail[0].startswith("AssertionError:")
    assert "       |                         ^" in report.first.detail


def test_vitest_reader_reads_file_level_failure_heading() -> None:
    """A file-level Vitest failure uses the file path as its title."""
    log_text = (FIXTURES / "vitest-file-level.txt").read_text(encoding="utf-8")

    report = VitestReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "tests/file-level.test.js"
    assert report.first.title == "tests/file-level.test.js"
    assert report.first.line == 4
    assert report.first.column == 12


def test_vitest_reader_extracts_path_from_thrown_error_frame() -> None:
    """A thrown-error frame keeps the path separate from its method label."""
    log_text = (FIXTURES / "vitest-thrown-error.txt").read_text(encoding="utf-8")

    report = VitestReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "lib/number/round.test.js"
    assert report.first.line == 9
    assert report.first.column == 25


def test_vitest_reader_returns_none_for_clean_output() -> None:
    """A clean Vitest run has no failure report to return."""
    log_text = (FIXTURES / "vitest-clean.txt").read_text(encoding="utf-8")

    assert VitestReader().read(log_text) is None


def test_read_failure_report_falls_back_for_unrecognised_vitest_output() -> None:
    """Vitest output without failure sections falls back to the last log lines."""
    log_text = (FIXTURES / "vitest-fallback.txt").read_text(encoding="utf-8")

    report = read_failure_report(["vitest", "run"], log_text)

    assert report.recognised is False
    assert report.first is None
    assert report.tail == tuple(log_text.splitlines())
