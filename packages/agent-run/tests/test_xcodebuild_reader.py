"""Tests for actionable xcodebuild failure output."""

from pathlib import Path

import pytest
from agent_run.readers import read_failure_report
from agent_run.readers.xcodebuild import XcodebuildReader

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("xcodebuild-success-build.txt", ["Build succeeded"]),
        (
            "xcodebuild-success-build-for-testing.txt",
            ["Test build succeeded"],
        ),
        (
            "xcodebuild-success-test.txt",
            [
                "Tests passed: 2 tests",
                "Results: /project/Lew Timer/.build/DerivedData/Logs/Test/Test-LewTimer-2026.09.23_13-39-24-+0100.xcresult",
            ],
        ),
    ],
)
def test_summarises_success(fixture: str, expected: list[str]) -> None:
    """A passing Xcode run reports the result after install noise."""
    log_text = (FIXTURES / fixture).read_text(encoding="utf-8")

    assert XcodebuildReader().summarise(log_text) == expected


def test_summarises_test_counts_across_bundles() -> None:
    """XCTest and Swift Testing totals both contribute to the final count."""
    log_text = (
        "Test Suite 'All tests' passed at 2026-09-23 13:39:32.432.\n"
        "\t Executed 2 tests, with 0 failures\n"
        "Test run with 3 tests in 1 suite passed\n"
        "** TEST SUCCEEDED **\n"
    )

    assert XcodebuildReader().summarise(log_text) == ["Tests passed: 5 tests"]


def test_summarises_one_test_with_singular_wording() -> None:
    """A one-test run uses singular wording."""
    log_text = "Test run with 1 test in 0 suites passed\n** TEST SUCCEEDED **\n"

    assert XcodebuildReader().summarise(log_text) == ["Tests passed: 1 test"]


def test_summarises_nested_xctest_suites_once() -> None:
    """Nested XCTest suite totals do not count the same test again."""
    log_text = (FIXTURES / "xcodebuild-success-xctest.txt").read_text(encoding="utf-8")

    assert XcodebuildReader().summarise(log_text) == ["Tests passed: 1 test"]


def test_falls_back_when_passing_test_log_has_no_total() -> None:
    """A successful test marker alone cannot establish the number of tests."""
    assert XcodebuildReader().summarise("** TEST SUCCEEDED **\n") is None


@pytest.mark.parametrize(
    "argv", [["xcodebuild", "build"], ["/usr/bin/xcrun", "xcodebuild", "test"]]
)
def test_matches_xcodebuild_launchers(argv: list[str]) -> None:
    """Direct and xcrun commands use the reader."""
    assert XcodebuildReader().matches(argv)


@pytest.mark.parametrize("argv", [[], ["xcrun", "swift"], ["other", "xcodebuild"]])
def test_ignores_other_commands(argv: list[str]) -> None:
    """Unrelated commands retain the shared tail fallback."""
    assert not XcodebuildReader().matches(argv)


@pytest.mark.parametrize(
    "fixture", ["xcodebuild-build.txt", "xcodebuild-test-compile.txt"]
)
def test_reads_swift_compile_error_before_xcode_install_noise(fixture: str) -> None:
    """The source error wins over startup noise and a repeated test summary."""
    log_text = (FIXTURES / fixture).read_text(encoding="utf-8")

    report = read_failure_report(["xcodebuild", "test"], log_text)

    assert report.recognised
    assert report.first is not None
    assert (
        report.first.path
        == "/project/LewTimer/LewTimerCore/Sources/LewTimerCore/CorePlaceholder.swift"
    )
    assert report.first.line == 11
    assert report.first.column == 35
    assert (
        report.first.title
        == "cannot convert value of type 'String' to specified type 'Int'"
    )
    assert report.more == ()


def test_reads_bare_code_signing_error() -> None:
    """A code-signing error wins over the generic test failure summary."""
    log_text = (FIXTURES / "xcodebuild-code-sign.txt").read_text(encoding="utf-8")

    report = XcodebuildReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path is None
    assert report.first.title.startswith("Cannot code sign because")


def test_reads_testing_failed_reasons_without_diagnostics() -> None:
    """Indented reasons remain useful when no source error was printed."""
    report = XcodebuildReader().read(
        "Testing failed:\n\tThe build failed.\n\n** TEST FAILED **\n"
    )

    assert report is not None
    assert report.first is not None
    assert report.first.title == "The build failed."


@pytest.mark.parametrize("marker", ["** BUILD FAILED **", "** TEST FAILED **"])
def test_reads_indented_failed_build_step(marker: str) -> None:
    """The final failed-step block supplies a reason when no error was printed."""
    report = XcodebuildReader().read(
        f"{marker}\n\nThe following build commands failed:\n\tSwiftCompile normal arm64\n"
    )

    assert report is not None
    assert report.first is not None
    assert report.first.title == "SwiftCompile normal arm64"


def test_ignores_xcode_install_noise_without_a_real_error() -> None:
    """An install warning leaves the log to the generic tail fallback."""
    log_text = (
        "2026-09-23 13:20:25.624 xcodebuild[1:2] [MT] IDERunDestination: error: Supported platforms empty\n"
        "  Details: error: CoreSimulator is out of date\n"
    )

    report = read_failure_report(["xcodebuild", "build"], log_text)

    assert XcodebuildReader().read(log_text) is None
    assert report.recognised is False
    assert report.first is None


def test_reads_source_error_without_line_number() -> None:
    """A file named without coordinates remains a source location."""
    report = XcodebuildReader().read("/project/File.swift: error: module not found\n")

    assert report is not None
    assert report.first is not None
    assert report.first.path == "/project/File.swift"
    assert report.first.line is None
    assert report.first.column is None
    assert report.first.title == "module not found"


def test_reads_linker_tools_as_messages_without_source_paths() -> None:
    """The linker and compiler tool names are not mistaken for files."""
    report = XcodebuildReader().read(
        "Undefined symbols for architecture arm64:\n"
        "ld: error: symbol(s) not found for architecture arm64\n"
        "clang: error: linker command failed with exit code 1\n"
    )

    assert report is not None
    assert report.first is not None
    assert report.first.path is None
    assert report.first.title == "symbol(s) not found for architecture arm64"
    assert len(report.more) == 1
    assert report.more[0].path is None
    assert report.more[0].title == "linker command failed with exit code 1"


def test_reads_xctest_failure_from_real_log() -> None:
    """A test case summary does not duplicate its source assertion failure."""
    log_text = (FIXTURES / "xcodebuild-xctest-failure.txt").read_text(encoding="utf-8")

    report = XcodebuildReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert (
        report.first.path
        == "/project/LewTimer/LewTimerTests/FixtureXCTestFailure.swift"
    )
    assert report.first.line == 5
    assert report.first.title.startswith(
        "-[LewTimerTests.FixtureXCTestFailure testNumbersDiffer]"
    )
    assert "numbers differ" in report.first.title
    assert report.more == ()


def test_reads_swift_testing_issues_from_real_log() -> None:
    """Each issue keeps its source location without failed-test summaries."""
    log_text = (FIXTURES / "xcodebuild-swift-testing-failure.txt").read_text(
        encoding="utf-8"
    )

    report = XcodebuildReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "FixtureSwiftTestingFailure.swift"
    assert report.first.line == 5
    assert report.first.column == 9
    assert "Expectation failed: 1 == 2" in report.first.title
    assert len(report.more) == 1
    assert report.more[0].path == "FixtureSwiftTestingFailure.swift"
    assert report.more[0].line == 9
    assert report.more[0].column == 13


def test_reads_linker_failure_from_real_log() -> None:
    """The missing symbol and its caller appear before the generic link failure."""
    log_text = (FIXTURES / "xcodebuild-linker-failure.txt").read_text(encoding="utf-8")

    report = XcodebuildReader().read(log_text)

    assert report is not None
    assert report.first is not None
    assert report.first.path is None
    assert report.first.title == "Undefined symbol: _lewtimer_fixture_missing_symbol"
    assert any(
        "FixtureLinkerFailure.testMissingSymbol" in line for line in report.first.detail
    )
    assert any("linker command failed" in failure.title for failure in report.more)
