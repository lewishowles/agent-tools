"""Tests for the agent-run entry point and result output."""

import json
import signal
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from agent_run.cli import main
from agent_run.database import connect_database
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
    """The run command saves combined output and reports the run details."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    command = [
        sys.executable,
        "-c",
        "import sys; print('out', flush=True); print('err', file=sys.stderr)",
    ]
    text_exit_code = main(["run", "--timeout", "5", "--", *command])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert "out\nerr\n" not in text_output.out
    assert "exit status: 0" in text_output.out
    assert "run ID:" in text_output.out
    assert "log path:" in text_output.out
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
    assert result["data"]["duration_seconds"] >= 0
    assert result["data"]["run_id"]
    log_path = Path(result["data"]["log_path"])
    assert log_path.read_text() == "out\nerr\n"
    assert json_output.err == ""


def test_cli_run_maps_failures_to_contract_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run command maps child failure, timeout, and missing executables."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

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
    assert failed_result["error"]["code"] == "check-failed"
    assert "Command exited with status 7." in failed_result["error"]["message"]
    assert "run ID:" in failed_result["error"]["message"]
    assert "log path:" in failed_result["error"]["message"]
    failed_data = failed_result["error"]["data"]
    assert failed_data["exit_status"] == 7
    assert failed_data["timed_out"] is False
    assert Path(failed_data["log_path"]).read_text() == ""
    assert failed_output.err == ""

    connection = connect_database(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        connection.close()

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
    assert timeout_result["error"]["code"] == "check-failed"
    assert "Command killed after 0.1 seconds." in timeout_result["error"]["message"]
    assert "run ID:" in timeout_result["error"]["message"]
    assert "log path:" in timeout_result["error"]["message"]
    timeout_data = timeout_result["error"]["data"]
    assert timeout_data["timed_out"] is True
    assert Path(timeout_data["log_path"]).read_text() == ""
    assert timeout_output.err == ""

    connection = connect_database(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    finally:
        connection.close()

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

    log_directory = database_path.parent / "agent-run-logs"
    assert len(list(log_directory.iterdir())) == 2

    connection = connect_database(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    finally:
        connection.close()


def test_cli_run_non_executable_file_removes_empty_log_and_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-executable command leaves no empty log or run record."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    command_path = root / "not-executable"
    command_path.write_text("#!/bin/sh\n")
    command_path.chmod(0o600)

    exit_code = main(["run", "--timeout", "5", "--json", "--", str(command_path)])
    output = capsys.readouterr()
    result = json.loads(output.out)

    assert exit_code == 3
    assert result["ok"] is False
    assert result["error"]["code"] == "environment"
    assert output.err == ""
    assert list((database_path.parent / "agent-run-logs").iterdir()) == []

    connection = connect_database(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        connection.close()


def test_cli_run_defaults_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run command uses the default timeout when none is supplied."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["run", "--json", "--", "echo"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert result["data"]["exit_status"] == 0
    assert captured.err == ""

    connection = connect_database(database_path)
    try:
        timeout = connection.execute("SELECT timeout_seconds FROM runs").fetchone()[0]
    finally:
        connection.close()

    assert timeout == 120


def test_cli_named_run_uses_saved_directory_and_timeout_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Named runs use stored values unless the CLI supplies a timeout override."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    command = [sys.executable, "-c", "print('ok')"]
    assert main(["add", "default", "--", *command]) == 0
    capsys.readouterr()

    default_exit_code = main(["run", "default", "--json"])
    default_output = capsys.readouterr()
    default_result = json.loads(default_output.out)

    assert default_exit_code == 0
    assert default_result["ok"] is True
    assert default_result["data"]["working_directory"] == str(root)

    assert (
        main(
            [
                "add",
                "build",
                "--cwd",
                "tools",
                "--timeout",
                "5",
                "--",
                *command,
            ]
        )
        == 0
    )
    capsys.readouterr()

    saved_exit_code = main(["run", "build", "--json"])
    saved_output = capsys.readouterr()
    saved_result = json.loads(saved_output.out)

    assert saved_exit_code == 0
    assert saved_result["ok"] is True
    assert saved_result["data"]["working_directory"] == str(root / "tools")

    override_exit_code = main(["run", "build", "--timeout", "2", "--json"])
    override_output = capsys.readouterr()
    override_result = json.loads(override_output.out)

    assert override_exit_code == 0
    assert override_result["ok"] is True

    connection = connect_database(database_path)
    try:
        timeouts = [
            row[0]
            for row in connection.execute(
                "SELECT timeout_seconds FROM runs ORDER BY rowid"
            ).fetchall()
        ]
    finally:
        connection.close()

    assert timeouts == [120.0, 5.0, 2.0]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["run", "build", "--json", "--", "echo"],
            "named commands cannot include arguments after --",
        ),
        (
            ["run", "build", "--cwd", ".", "--json"],
            "named commands use their stored working directory",
        ),
    ],
)
def test_cli_named_run_rejects_direct_only_arguments(
    arguments: list[str],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Named runs reject direct-run arguments before opening the database."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["add", "build", "--", "echo"]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit) as error:
        main(arguments)

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result == {
        "ok": False,
        "error": {"code": "usage", "message": message},
    }


def test_cli_named_run_ignores_an_empty_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty separator is not treated as named-run arguments."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["add", "build", "--", "echo"]) == 0
    capsys.readouterr()

    exit_code = main(["run", "build", "--json", "--"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert captured.err == ""


def test_cli_run_returns_130_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An interrupted run returns the dedicated interrupt exit status."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    monkeypatch.setattr(
        "agent_run.cli.run_command", Mock(side_effect=KeyboardInterrupt)
    )

    exit_code = main(["run", "--timeout", "5", "--", "echo"])

    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert "Error: Command interrupted." in captured.err
    assert "run ID:" in captured.err
    assert "log path:" in captured.err

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        row = connection.execute("SELECT exit_status, timed_out FROM runs").fetchone()
    finally:
        connection.close()

    assert row == (-signal.SIGINT, 0)


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
    """Text-mode failures report the status and log without child output."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

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
    assert captured.out == ""
    assert "Error: Command exited with status 9." in captured.err
    assert "run ID:" in captured.err
    assert "log path:" in captured.err


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


def test_json_error_includes_optional_data() -> None:
    """JSON errors include structured data only when the caller supplies it."""
    stdout = StringIO()
    stderr = StringIO()

    exit_code = render_error(
        json_mode=True,
        code="check-failed",
        message="The command failed.",
        data={"run_id": "run-1", "log_path": "/tmp/run-1.log"},
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert json.loads(stdout.getvalue()) == {
        "ok": False,
        "error": {
            "code": "check-failed",
            "message": "The command failed.",
            "data": {"run_id": "run-1", "log_path": "/tmp/run-1.log"},
        },
    }
    assert stderr.getvalue() == ""


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
