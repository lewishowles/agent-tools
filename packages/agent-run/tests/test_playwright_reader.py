"""Tests for the Playwright failure reader."""

from pathlib import Path

import pytest
from agent_run.readers import read_failure_report, summarise_success
from agent_run.readers.playwright import PlaywrightReader

# These fixtures are complete logs from recorded Playwright component test runs.
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "argv",
    [
        ["playwright", "test"],
        ["/opt/bin/playwright", "test", "example.spec.ts"],
        ["npx", "playwright", "test"],
        ["bunx", "playwright", "test"],
        ["npm", "exec", "playwright", "test"],
        ["pnpm", "playwright", "test"],
        ["pnpm", "exec", "playwright", "test"],
        ["pnpm", "dlx", "playwright", "test"],
        ["yarn", "playwright", "test"],
        ["yarn", "exec", "playwright", "test"],
    ],
)
def test_matches_playwright_test_launchers(argv: list[str]) -> None:
    """The reader recognises direct and package-launched test commands."""
    assert PlaywrightReader().matches(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["playwright"],
        ["playwright", "show-report"],
        ["npx", "playwright", "codegen"],
        ["npm", "playwright", "test"],
        ["npm", "dlx", "playwright", "test"],
        ["pnpm", "exec", "vitest", "run"],
        ["yarn", "test"],
    ],
)
def test_ignores_other_commands(argv: list[str]) -> None:
    """Only Playwright test logs go to this reader."""
    assert PlaywrightReader().matches(argv) is False


def test_reads_one_real_failure() -> None:
    """A numbered failure includes its test name, location, error, and code frame."""
    log_text = (FIXTURES / "playwright-failures.txt").read_text(encoding="utf-8")
    first_failure = log_text.split("  2) [chromium]", maxsplit=1)[0]

    report = PlaywrightReader().read(first_failure)

    assert report is not None
    assert report.first is not None
    assert report.first.path == "src/components/messaging/pill-badge/pill-badge.pw.js"
    assert report.first.line == 66
    assert report.first.column == 3
    assert report.first.title == "pill-badge › supports colour: primary"
    assert (
        report.first.detail[0]
        == "    Error: expect(locator).toHaveClass(expected) failed"
    )
    assert (
        '    > 70 | \t\t\t\tawait expect(page.getByTestId("pill-badge")).toHaveClass(new RegExp(className));'
        in report.first.detail
    )
    assert report.more == ()


def test_reads_a_padded_failure_heading() -> None:
    """The dashes Playwright adds after a short heading are not part of the test title."""
    heading = "  1) [chromium] › tests/a.spec.ts:3:5 › has title"
    padded_heading = heading + " " + "─" * (100 - len(heading) - 1)

    report = PlaywrightReader().read(padded_heading + "\n\n    Error: failed")

    assert report is not None
    assert report.first is not None
    assert report.first.title == "has title"


def test_reads_a_failure_without_a_project_name() -> None:
    """A failure without a named project still keeps its location and title."""
    report = PlaywrightReader().read(
        "  1) tests/a.spec.ts:3:5 › has title ───\n\n    Error: failed"
    )

    assert report is not None
    assert report.first is not None
    assert report.first.path == "tests/a.spec.ts"
    assert report.first.line == 3
    assert report.first.column == 5
    assert report.first.title == "has title"


def test_lists_several_failures_from_the_real_log() -> None:
    """The first failure has detail and the second keeps its test name and location."""
    log_text = (FIXTURES / "playwright-failures.txt").read_text(encoding="utf-8")

    report = read_failure_report(["npx", "playwright", "test"], log_text)

    assert report.recognised is True
    assert report.first is not None
    assert report.first.title == "pill-badge › supports colour: primary"
    assert (
        report.first.detail[0].strip()
        == "Error: expect(locator).toHaveClass(expected) failed"
    )
    assert report.truncated is True
    assert any(line.lstrip().startswith("> 70 |") for line in report.first.detail)
    assert any(
        line.lstrip().startswith("|") and "^" in line for line in report.first.detail
    )
    assert len(report.more) == 1
    assert report.more[0].path == "src/components/messaging/pill-badge/pill-badge.pw.js"
    assert report.more[0].line == 66
    assert report.more[0].column == 3
    assert report.more[0].title == "pill-badge › supports colour: danger"
    assert report.more[0].detail == ()


def test_summarises_a_real_passing_run() -> None:
    """A passing run shows its final count and duration."""
    log_text = (FIXTURES / "playwright-pass.txt").read_text(encoding="utf-8")

    summary = summarise_success(["npx", "playwright", "test"], log_text)

    assert summary.recognised is True
    assert summary.lines == ["18 passed (14.0s)"]
    assert PlaywrightReader().read(log_text) is None


def test_summarises_all_closing_count_types() -> None:
    """Failed, flaky, passed, and skipped counts can all appear at the end."""
    log_text = "  2 failed\n  1 flaky\n  16 passed (21.6s)\n  3 skipped"

    assert PlaywrightReader().summarise(log_text) == [
        "2 failed",
        "1 flaky",
        "16 passed (21.6s)",
        "3 skipped",
    ]


def test_unrecognised_output_falls_back_to_the_log_tail() -> None:
    """A Playwright command with no numbered failures keeps its final log lines."""
    log_text = "Playwright could not start\nMissing browser executable\n"

    report = read_failure_report(["playwright", "test"], log_text)

    assert report.recognised is False
    assert report.first is None
    assert report.tail == ("Playwright could not start", "Missing browser executable")
