"""Tests for the pytest failure reader."""

from pathlib import Path

import pytest
from agent_run.readers import read_failure_report
from agent_run.readers.pytest import PytestReader

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "tests"],
        ["/opt/venv/bin/pytest", "-q"],
        ["python", "-m", "pytest", "tests"],
        ["python3", "-m", "pytest"],
        ["uv", "run", "pytest", "tests"],
    ],
)
def test_pytest_reader_matches_supported_launchers(argv: list[str]) -> None:
    """The reader recognises the supported direct pytest launchers."""
    assert PytestReader().matches(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["python", "script.py"],
        ["uv", "run", "ruff", "check"],
        ["scripts/run-tests"],
    ],
)
def test_pytest_reader_ignores_other_launchers(argv: list[str]) -> None:
    """The reader leaves wrappers and other commands to the fallback."""
    assert PytestReader().matches(argv) is False


def test_pytest_reader_reads_the_first_failure_and_short_summary() -> None:
    """The first block is detailed and later failures stay one line each."""
    log_text = (FIXTURES / "pytest-failures.txt").read_text(encoding="utf-8")

    report = PytestReader().read(log_text)

    assert report is not None
    assert report.recognised is True
    assert report.first is not None
    assert report.first.path == "tests/test_example.py"
    assert report.first.line == 4
    assert report.first.column is None
    assert report.first.title == "assert 1 == 2"
    assert report.first.detail == (
        "    def test_example():",
        ">       assert 1 == 2",
        "E       assert 1 == 2",
        "E       assert 1 == 2",
        "",
        "tests/test_example.py:4: AssertionError",
    )
    assert len(report.more) == 1
    assert report.more[0].path == "tests/test_other.py"
    assert report.more[0].title == "assert False"
    assert report.more[0].detail == ()


def test_pytest_reader_returns_none_for_collection_errors() -> None:
    """Collection errors without a failures section remain parser fallback."""
    log_text = (FIXTURES / "pytest-collection-error.txt").read_text(encoding="utf-8")

    assert PytestReader().read(log_text) is None


def test_pytest_reader_keeps_error_summary_path() -> None:
    """An error summary without a node id still provides its source path."""
    log_text = (FIXTURES / "pytest-error-summary.txt").read_text(encoding="utf-8")

    report = PytestReader().read(log_text)

    assert report is not None
    assert len(report.more) == 1
    assert report.more[0].path == "tests/test_collection.py"
    assert report.more[0].title == "ImportError"


def test_pytest_reader_ignores_captured_output_locations() -> None:
    """Captured output cannot replace the source location in a failure block."""
    log_text = (FIXTURES / "pytest-captured-output.txt").read_text(encoding="utf-8")

    report = PytestReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "tests/test_server.py"
    assert report.first.line == 12
    assert report.first.title == "AssertionError"


def test_pytest_error_class_without_e_line_keeps_the_fallback_detail_window() -> None:
    """A pytest error class does not use Vitest's line-zero anchor."""
    detail = (
        "AssertionError: values differ",
        *(f"traceback line {index}" for index in range(20)),
    )
    log_text = "\n".join(
        (
            "=================================== FAILURES ===================================",
            "_______________________________ test_failure ________________________________",
            *detail,
        )
    )

    report = read_failure_report(["pytest"], log_text)

    assert report.first is not None
    assert len(report.first.detail) == 20
    assert report.first.detail[0] == "traceback line 0"


def test_pytest_long_diff_keeps_the_code_frame_and_last_error_line() -> None:
    """A long pytest diff keeps both reader anchors in the bounded detail."""
    detail = (">       assert a == b",) + tuple(
        f"E       diff line {index}" for index in range(30)
    )
    log_text = "\n".join(
        (
            "=================================== FAILURES ===================================",
            "_______________________________ test_failure ________________________________",
            *detail,
        )
    )

    report = read_failure_report(["pytest"], log_text)

    assert report.first is not None
    assert len(report.first.detail) == 20
    assert ">       assert a == b" in report.first.detail
    assert "E       diff line 29" in report.first.detail
    assert report.truncated is True
