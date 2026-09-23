"""Tests for the vp check failure reader."""

from pathlib import Path

import pytest
from agent_run.readers import read_failure_report
from agent_run.readers.vp_check import VpCheckReader

FIXTURES = Path(__file__).parent / "fixtures"


def test_summarises_passing_checks() -> None:
    """The format and lint confirmations remain visible together."""
    log_text = (FIXTURES / "vp-check-success.txt").read_text(encoding="utf-8")

    assert VpCheckReader().summarise(log_text) == [
        "pass: All 5 files are correctly formatted (21ms, 8 threads)",
        "pass: Found no warnings or lint errors in 3 files (3ms, 8 threads)",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["vp", "check"],
        ["/opt/bin/vp", "check", "src"],
        ["npx", "vp", "check"],
        ["pnpm", "exec", "vp", "check"],
        ["npm", "exec", "vp", "check", "--no-fmt"],
    ],
)
def test_vp_check_reader_matches_supported_launchers(argv: list[str]) -> None:
    """The reader recognises vp check through supported launchers."""
    assert VpCheckReader().matches(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["vp", "test"],
        ["vp", "lint"],
        ["npx", "vitest"],
        ["pnpm", "exec", "vitest"],
        ["npm", "exec", "vp", "test"],
        ["scripts/run-check"],
    ],
)
def test_vp_check_reader_ignores_other_launchers(argv: list[str]) -> None:
    """The reader leaves other vp subcommands and wrappers to the fallback."""
    assert VpCheckReader().matches(argv) is False


def test_vp_check_reader_reads_format_failure_files() -> None:
    """A format failure reports each listed path without a source location."""
    log_text = (FIXTURES / "vp-check-format-single.txt").read_text(encoding="utf-8")

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.recognised is True
    assert report.first is not None
    assert report.first.path == "src/format.ts"
    assert report.first.line is None
    assert report.first.column is None
    assert report.first.title == "Formatting issues found"
    assert report.first.detail == ()
    assert report.more == ()


def test_vp_check_reader_reads_format_failure_for_each_file() -> None:
    """A format failure listing several files reports every path."""
    log_text = (FIXTURES / "vp-check-format-several.txt").read_text(encoding="utf-8")

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/format.ts"
    assert report.first.title == "Formatting issues found"
    assert report.first.line is None
    assert report.first.column is None
    assert report.first.detail == ()
    assert len(report.more) == 1
    assert report.more[0].path == "src/other-format.ts"
    assert report.more[0].title == "Formatting issues found"
    assert report.more[0].line is None
    assert report.more[0].column is None
    assert report.more[0].detail == ()


def test_vp_check_reader_reads_format_failure_when_lint_is_enabled() -> None:
    """vp prints no lint output after a format failure, so only the formatting paths are reported."""
    log_text = (FIXTURES / "vp-check-format-with-lint.txt").read_text(encoding="utf-8")

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/unformatted.ts"
    assert report.first.title == "Formatting issues found"
    assert report.more == ()


def test_vp_check_reader_reads_lint_failure_in_full() -> None:
    """The first lint diagnostic includes its location and code frame."""
    log_text = (FIXTURES / "vp-check-lint-single.txt").read_text(encoding="utf-8")

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/lint.ts"
    assert report.first.line == 1
    assert report.first.column == 15
    assert report.first.title == "Unexpected token"
    assert report.first.detail == (
        " 1 | const value = ;",
        "   :               ^",
    )
    assert report.more == ()


def test_vp_check_reader_keeps_diagnostics_without_locations() -> None:
    """A diagnostic without a location keeps its detail and its position in the report.

    vp has not been seen to print one, so the fixture is the single lint output with
    its location line removed.
    """
    log_text = (FIXTURES / "vp-check-no-location.txt").read_text(encoding="utf-8")

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path is None
    assert report.first.line is None
    assert report.first.column is None
    assert report.first.title == "Unexpected token"
    assert report.first.detail == (
        " 1 | const value = ;",
        "   :               ^",
    )
    assert len(report.more) == 1
    assert report.more[0].path == "src/lint.ts"
    assert report.more[0].line == 1
    assert report.more[0].column == 19


def test_vp_check_reader_skips_warning_before_type_failure() -> None:
    """Warnings do not become failures or displace the first error."""
    log_text = (FIXTURES / "vp-check-type-warning-before-error.txt").read_text(
        encoding="utf-8"
    )

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/type.ts"
    assert report.first.line == 1
    assert report.first.column == 7
    assert report.first.title == (
        "typescript(TS2322): Type 'number' is not assignable to type 'string'."
    )
    assert report.first.detail == (
        " 1 | const typed: string = 42;",
        "   :       ^^^^^",
    )
    assert report.more == ()


def test_vp_check_reader_ignores_format_pass_and_warnings() -> None:
    """A passing format check does not hide the first lint or type error."""
    log_text = (FIXTURES / "vp-check-combined.txt").read_text(encoding="utf-8")

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/type.ts"
    assert report.first.title.startswith("typescript(TS2322):")
    assert len(report.first.detail) == 2
    assert report.more == ()


def test_vp_check_reader_reads_syntax_error_when_formatting_cannot_start() -> None:
    """A syntax error remains the first failure when formatting cannot start."""
    log_text = (FIXTURES / "vp-check-format-could-not-start.txt").read_text(
        encoding="utf-8"
    )

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.recognised is True
    assert report.first is not None
    assert report.first.path == "src/lint.ts"
    assert report.first.line == 1
    assert report.first.column == 19
    assert report.first.title == "Unexpected token"
    assert report.first.detail == (
        " 1 | const lintValue = ;",
        "   :                   ^",
    )


def test_vp_check_reader_lists_later_errors_without_detail() -> None:
    """Later errors keep locations and titles, including the box-drawing location line."""
    log_text = (FIXTURES / "vp-check-several.txt").read_text(encoding="utf-8")

    report = VpCheckReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.title == "Unexpected token"
    assert len(report.first.detail) == 2
    assert len(report.more) == 1
    assert report.more[0].path == "src/type.ts"
    assert report.more[0].line == 1
    assert report.more[0].column == 7
    assert report.more[0].title.startswith("typescript(TS2322):")
    assert report.more[0].detail == ()


def test_vp_check_reader_returns_the_last_frame_anchor() -> None:
    """The detail anchor points to the last frame line."""
    detail = (
        " 1 | const value = ;",
        "   :               ^",
    )

    assert VpCheckReader().detail_anchors(detail) == (1, None)


def test_vp_check_reader_returns_none_for_clean_output() -> None:
    """A clean vp check has no failure report to return."""
    log_text = (FIXTURES / "vp-check-clean.txt").read_text(encoding="utf-8")

    assert VpCheckReader().read(log_text) is None


def test_read_failure_report_selects_vp_check_reader() -> None:
    """The registered reader returns a parsed report for vp check output."""
    log_text = (FIXTURES / "vp-check-lint-single.txt").read_text(encoding="utf-8")

    report = read_failure_report(["vp", "check"], log_text)

    assert report.recognised is True
    assert report.first is not None
    assert report.first.title == "Unexpected token"


def test_read_failure_report_falls_back_for_unrecognised_vp_check_output() -> None:
    """Output without a known vp failure structure uses the shared fallback."""
    log_text = (FIXTURES / "vp-check-fallback.txt").read_text(encoding="utf-8")

    report = read_failure_report(["vp", "check"], log_text)

    assert report.recognised is False
    assert report.first is None
    assert report.tail == tuple(log_text.splitlines())
