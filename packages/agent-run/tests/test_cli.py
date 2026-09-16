"""Tests for the agent-run entry point and result output."""

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from agent_run.cli import main
from agent_run.output import render_error, render_success


def _initialise_repository(path: Path) -> Path:
    """Create an empty temporary Git repository for a CLI test."""
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)

    return path


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


def test_cli_run_success_supports_text_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run command returns combined output in both output modes."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    command = [
        sys.executable,
        "-c",
        "import sys; print('out', flush=True); print('err', file=sys.stderr)",
    ]
    text_exit_code = main(["run", "--timeout", "5", "--", *command])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert text_output.out == "out\nerr\nexit status: 0\n"
    assert text_output.err == ""

    json_exit_code = main(["run", "--timeout", "5", "--json", "--", *command])
    json_output = capsys.readouterr()
    result = json.loads(json_output.out)

    assert json_exit_code == 0
    assert result["ok"] is True
    assert result["data"]["argv"] == command
    assert result["data"]["working_directory"] == str(root)
    assert result["data"]["exit_status"] == 0
    assert result["data"]["timed_out"] is False
    assert result["data"]["output"] == "out\nerr\n"
    assert result["data"]["duration_seconds"] >= 0
    assert json_output.err == ""


def test_cli_run_maps_failures_to_contract_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run command maps child failure, timeout, and missing executables."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    failed_exit_code = main(
        [
            "run",
            "--timeout",
            "5",
            "--json",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(7)",
        ]
    )
    failed_output = capsys.readouterr()
    failed_result = json.loads(failed_output.out)

    assert failed_exit_code == 1
    assert failed_result["ok"] is False
    assert failed_result["error"] == {
        "code": "check-failed",
        "message": "Command exited with status 7.",
    }
    assert failed_output.err == ""

    timeout_exit_code = main(
        [
            "run",
            "--timeout",
            "0.1",
            "--json",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
        ]
    )
    timeout_output = capsys.readouterr()
    timeout_result = json.loads(timeout_output.out)

    assert timeout_exit_code == 1
    assert timeout_result["ok"] is False
    assert timeout_result["error"] == {
        "code": "check-failed",
        "message": "Command killed after 0.1 seconds.",
    }
    assert timeout_output.err == ""

    missing_exit_code = main(
        ["run", "--timeout", "5", "--json", "--", "missing-agent-run-command"]
    )
    missing_output = capsys.readouterr()
    missing_result = json.loads(missing_output.out)

    assert missing_exit_code == 3
    assert missing_result["ok"] is False
    assert missing_result["error"]["code"] == "environment"
    assert "missing-agent-run-command" in missing_result["error"]["message"]
    assert missing_output.err == ""


def test_cli_run_requires_timeout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run command rejects a missing timeout before executing anything."""
    with pytest.raises(SystemExit) as error:
        main(["run", "--json", "--", "echo"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result == {
        "ok": False,
        "error": {
            "code": "usage",
            "message": "the following arguments are required: --timeout",
        },
    }
    assert "agent-run run: error:" in captured.err


def test_cli_run_returns_130_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An interrupted run returns the dedicated interrupt exit status."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "agent_run.cli.run_command", Mock(side_effect=KeyboardInterrupt)
    )

    exit_code = main(["run", "--timeout", "5", "--", "echo"])

    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert captured.err == "Error: Command interrupted.\n"


def test_cli_run_maps_non_positive_timeout_to_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-positive timeout returns the usage error envelope."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    exit_code = main(["run", "--timeout", "0", "--json", "--", "echo"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 2
    assert result == {
        "ok": False,
        "error": {
            "code": "usage",
            "message": "Command timeout must be finite and greater than zero.",
        },
    }
    assert captured.err == ""


def test_cli_run_requires_arguments_after_separator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run command rejects an empty argument array."""
    with pytest.raises(SystemExit) as error:
        main(["run", "--timeout", "5", "--json", "--"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result == {
        "ok": False,
        "error": {
            "code": "usage",
            "message": "the following arguments are required: ARGV",
        },
    }
    assert "agent-run run: error:" in captured.err


def test_cli_run_text_failure_prints_output_before_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Text-mode failure output is shown before the status error line."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    exit_code = main(
        [
            "run",
            "--timeout",
            "5",
            "--",
            sys.executable,
            "-c",
            "print('captured', flush=True); raise SystemExit(9)",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == "captured\n"
    assert captured.err == "Error: Command exited with status 9.\n"


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
