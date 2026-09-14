"""Tests for the agent-run entry point and result output."""

import json
from io import StringIO

import pytest
from agent_run.cli import main
from agent_run.output import render_error, render_success


def test_bare_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    """Running agent-run with no arguments shows help and succeeds."""
    exit_code = main([])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Run project commands with bounded evidence." in captured.out
    assert captured.err == ""


def test_json_mode_returns_help_data(capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` alone returns help text inside a successful JSON envelope."""
    exit_code = main(["--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert "usage: agent-run" in result["data"]["help"]
    assert captured.err == ""


def test_json_help_returns_help_data(capsys: pytest.CaptureFixture[str]) -> None:
    """`--json --help` returns the help text inside one JSON envelope."""
    exit_code = main(["--json", "--help"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert "usage: agent-run" in result["data"]["help"]
    assert captured.err == ""


def test_text_mode_renders_result() -> None:
    """Text mode writes only the text, with one trailing newline, to stdout."""
    stdout = StringIO()
    stderr = StringIO()

    exit_code = render_success(
        json_mode=False,
        data={"status": "ready"},
        text="Ready",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Ready\n"
    assert stderr.getvalue() == ""


def test_json_mode_renders_result() -> None:
    """JSON mode keeps stdout to the envelope and sends diagnostics to stderr."""
    stdout = StringIO()
    stderr = StringIO()

    exit_code = render_success(
        json_mode=True,
        data={"status": "ready"},
        diagnostic="progress",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert json.loads(stdout.getvalue()) == {"ok": True, "data": {"status": "ready"}}
    assert stderr.getvalue() == "progress\n"


@pytest.mark.parametrize(
    ("error_code", "expected_exit_code"),
    [
        ("usage", 2),
        ("not-found", 1),
        ("check-failed", 1),
        ("environment", 3),
        ("internal", 3),
    ],
)
def test_error_codes_use_contract_exit_codes(
    error_code: str, expected_exit_code: int
) -> None:
    """Each contract error code returns its agreed exit status and envelope."""
    stdout = StringIO()
    stderr = StringIO()

    exit_code = render_error(
        json_mode=True,
        code=error_code,
        message="The requested result is unavailable.",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == expected_exit_code
    assert json.loads(stdout.getvalue()) == {
        "ok": False,
        "error": {
            "code": error_code,
            "message": "The requested result is unavailable.",
        },
    }


def test_json_usage_error_writes_envelope_to_stdout_and_diagnostic_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bad argument with `--json` still produces a parseable usage error."""
    with pytest.raises(SystemExit) as error:
        main(["--json", "--unknown"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "unrecognized arguments" in result["error"]["message"]
    assert "agent-run: error:" in captured.err


def test_text_error_writes_message_to_stderr() -> None:
    """Text errors leave stdout empty and return the contract exit status."""
    stdout = StringIO()
    stderr = StringIO()

    exit_code = render_error(
        json_mode=False,
        code="not-found",
        message="The requested result is unavailable.",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "Error: The requested result is unavailable.\n"
