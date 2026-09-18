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


def test_cli_named_file_list_run_appends_relative_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file-list command appends unique paths relative to its run directory."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    (root / "src").mkdir()
    (root / "src" / "one.py").write_text("one")
    (root / "src" / "two.py").write_text("two")
    (root / "src" / "three.py").write_text("three")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    command = [
        sys.executable,
        "-c",
        "import json, sys; print(json.dumps(sys.argv[1:]))",
    ]
    assert (
        main(
            [
                "add",
                "format",
                "--cwd",
                "tools",
                "--capability",
                "file-list",
                "--",
                *command,
            ]
        )
        == 0
    )
    capsys.readouterr()

    list_exit_code = main(["list"])
    list_output = capsys.readouterr()

    assert list_exit_code == 0
    assert "capability: file-list" in list_output.out

    exit_code = main(
        [
            "run",
            "format",
            "--file",
            "src/one.py",
            "--file",
            "src/one.py",
            "--file",
            "src/two.py",
            "--glob",
            "src/*.py",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert result["data"]["files"] == [
        "../src/one.py",
        "../src/two.py",
        "../src/three.py",
    ]
    assert result["data"]["argv"] == command + [
        "../src/one.py",
        "../src/two.py",
        "../src/three.py",
    ]
    assert captured.err == ""


def test_cli_file_targets_report_usage_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """File targets reject direct runs, unsupported commands, missing files, and unmatched globs."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "file.py").write_text("file")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["add", "plain", "--", "echo"]) == 0
    assert main(["add", "files", "--capability", "file-list", "--", "echo"]) == 0
    capsys.readouterr()

    unsupported_exit_code = main(["run", "plain", "--file", "file.py", "--json"])
    unsupported_output = capsys.readouterr()
    unsupported_result = json.loads(unsupported_output.out)

    assert unsupported_exit_code == 2
    assert unsupported_result["error"]["code"] == "usage"
    assert 'capability "none"' in unsupported_result["error"]["message"]
    assert (
        "agent-run edit plain --capability file-list"
        in unsupported_result["error"]["message"]
    )
    assert unsupported_output.err == ""

    missing_exit_code = main(["run", "files", "--file", "missing.py", "--json"])
    missing_output = capsys.readouterr()
    missing_result = json.loads(missing_output.out)

    assert missing_exit_code == 2
    assert missing_result["error"]["code"] == "usage"
    assert "missing.py" in missing_result["error"]["message"]
    assert missing_output.err == ""

    unmatched_glob_exit_code = main(
        ["run", "files", "--glob", "missing/**/*.py", "--json"]
    )
    unmatched_glob_output = capsys.readouterr()
    unmatched_glob_result = json.loads(unmatched_glob_output.out)

    assert unmatched_glob_exit_code == 2
    assert unmatched_glob_result["error"]["code"] == "usage"
    assert "missing/**/*.py" in unmatched_glob_result["error"]["message"]
    assert unmatched_glob_output.err == ""

    with pytest.raises(SystemExit) as error:
        main(["run", "--file", "file.py", "--json", "--", "echo"])

    direct_output = capsys.readouterr()
    direct_result = json.loads(direct_output.out)

    assert error.value.code == 2
    assert direct_result["error"]["code"] == "usage"
    assert "only valid for a named command" in direct_result["error"]["message"]
    assert "agent-run run: error:" in direct_output.err

    with pytest.raises(SystemExit) as glob_error:
        main(["run", "--glob", "*.py", "--json", "--", "echo"])

    glob_direct_output = capsys.readouterr()
    glob_direct_result = json.loads(glob_direct_output.out)

    assert glob_error.value.code == 2
    assert glob_direct_result["error"]["code"] == "usage"
    assert "only valid for a named command" in glob_direct_result["error"]["message"]
    assert "agent-run run: error:" in glob_direct_output.err


def test_cli_named_file_list_failure_includes_files_in_json_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed file-list run includes its resolved files in error data."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "file.py").write_text("file")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    command = [sys.executable, "-c", "raise SystemExit(4)"]
    assert main(["add", "check", "--capability", "file-list", "--", *command]) == 0
    capsys.readouterr()

    exit_code = main(["run", "check", "--file", "file.py", "--glob", "*.py", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["error"]["data"]["files"] == ["file.py"]
    assert captured.err == ""


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


def test_cli_run_json_failure_includes_unrecognised_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON failures keep a fallback tail for commands without a reader."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main(
        [
            "run",
            "--timeout",
            "5",
            "--json",
            "--",
            sys.executable,
            "-c",
            "print('captured', flush=True); raise SystemExit(9)",
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    failure = result["error"]["data"]["failure"]

    assert exit_code == 1
    assert result["ok"] is False
    assert failure["recognised"] is False
    assert failure["tail"]
    assert captured.err == ""


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
    assert "Failure output (last 1 line):" in captured.err


def test_cli_retrieves_saved_run_records_logs_and_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past-run commands read saved evidence without running the command again."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    command_path = root / "pytest"
    command_path.write_text(
        """#!/usr/bin/env python3
print('run preamble')
print('============================= FAILURES =============================')
print('____________________________ test_demo ______________________________')
print('E       AssertionError: values differ')
print('=========================== short test summary info ============================')
print('FAILED tests/test_demo.py::test_demo - values differ')
print('PASSED tests/test_ok.py::test_ok')
raise SystemExit(1)
"""
    )
    command_path.chmod(0o700)

    run_exit_code = main(["run", "--timeout", "5", "--json", "--", str(command_path)])
    run_output = capsys.readouterr()
    run_result = json.loads(run_output.out)
    run_id = run_result["error"]["data"]["run_id"]

    assert run_exit_code == 1

    runs_exit_code = main(["runs", "--json"])
    runs_output = capsys.readouterr()
    runs_result = json.loads(runs_output.out)

    assert runs_exit_code == 0
    assert runs_result["data"]["runs"][0]["run_id"] == run_id
    assert runs_result["data"]["runs"][0]["argv"] == [str(command_path)]

    runs_text_exit_code = main(["runs"])
    runs_text_output = capsys.readouterr()

    assert runs_text_exit_code == 0
    assert f"run ID: {run_id}" in runs_text_output.out
    assert f'command: ["{command_path}"]' in runs_text_output.out

    show_exit_code = main(["show", run_id, "--json"])
    show_output = capsys.readouterr()
    show_result = json.loads(show_output.out)

    assert show_exit_code == 0
    assert show_result["data"]["run_id"] == run_id
    assert show_result["data"]["exit_status"] == 1
    assert show_result["data"]["duration_seconds"] >= 0
    assert show_result["data"]["log_path"] == run_result["error"]["data"]["log_path"]

    show_text_exit_code = main(["show", run_id])
    show_text_output = capsys.readouterr()

    assert show_text_exit_code == 0
    assert f"run ID: {run_id}" in show_text_output.out
    assert f'command: ["{command_path}"]' in show_text_output.out
    assert "exit status: 1" in show_text_output.out
    assert "duration:" in show_text_output.out
    assert "log path:" in show_text_output.out

    log_exit_code = main(["log", run_id])
    log_output = capsys.readouterr()

    assert log_exit_code == 0
    assert "run preamble\n" in log_output.out
    assert "PASSED tests/test_ok.py::test_ok\n" in log_output.out

    log_json_exit_code = main(["log", run_id, "--json"])
    log_json_output = capsys.readouterr()
    log_json_result = json.loads(log_json_output.out)

    assert log_json_exit_code == 0
    assert log_json_result["data"]["log"] == log_output.out

    failure_exit_code = main(["failures", run_id])
    failure_output = capsys.readouterr()

    assert failure_exit_code == 0
    assert "AssertionError: values differ" in failure_output.out
    assert "run preamble" not in failure_output.out
    assert "PASSED tests/test_ok.py::test_ok" not in failure_output.out

    failure_json_exit_code = main(["failures", run_id, "--json"])
    failure_json_output = capsys.readouterr()
    failure_json_result = json.loads(failure_json_output.out)

    assert failure_json_exit_code == 0
    assert failure_json_result["data"]["run_id"] == run_id
    assert failure_json_result["data"]["failure"]["first"]["detail"] == [
        "E       AssertionError: values differ"
    ]
    assert failure_json_result["data"]["failure"]["tail"] == []


@pytest.mark.parametrize("command", ["show", "log", "failures"])
def test_cli_retrieval_rejects_an_unknown_run_id(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past-run commands return not-found for an unknown ID."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main([command, "missing-run", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert result == {
        "ok": False,
        "error": {
            "code": "not-found",
            "message": 'Run "missing-run" was not found.',
        },
    }
    assert captured.err == ""


@pytest.mark.parametrize("command", ["show", "log", "failures"])
@pytest.mark.parametrize("run_id", [None, "", "   "])
@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_retrieval_rejects_a_blank_run_id(
    command: str,
    run_id: str | None,
    json_mode: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past-run commands explain how to find a missing or blank run ID."""
    arguments = [command]

    if run_id is not None:
        arguments.append(run_id)

    if json_mode:
        arguments.append("--json")

    with pytest.raises(SystemExit) as error:
        main(arguments)

    captured = capsys.readouterr()
    message = "A run ID is required. Run `agent-run runs` to list saved run IDs."

    assert error.value.code == 2

    if json_mode:
        assert json.loads(captured.out) == {
            "ok": False,
            "error": {"code": "usage", "message": message},
        }
    else:
        assert captured.out == ""
        assert f"error: {message}" in captured.err


def test_cli_failures_reports_a_passed_run_without_reading_its_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passed runs report no failures without calling a failure reader."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    assert main(["run", "--json", "--", sys.executable, "-c", "print('passed')"]) == 0
    run_output = capsys.readouterr()
    run_id = json.loads(run_output.out)["data"]["run_id"]
    reader = Mock(side_effect=AssertionError("passed runs must not read failures"))
    monkeypatch.setattr("agent_run.cli.read_failure_report", reader)

    text_exit_code = main(["failures", run_id])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert text_output.out == "No failures: the run passed.\n"
    assert text_output.err == ""
    assert reader.call_count == 0

    json_exit_code = main(["failures", run_id, "--json"])
    json_output = capsys.readouterr()
    json_result = json.loads(json_output.out)

    assert json_exit_code == 0
    assert json_result["data"]["failure"] == {
        "recognised": False,
        "first": None,
        "more": [],
        "hidden_count": 0,
        "truncated": False,
        "tail": [],
    }
    assert json_output.err == ""


def test_cli_runs_text_reports_no_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The text run list names an empty repository clearly."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main(["runs"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "No runs recorded.\n"
    assert captured.err == ""


@pytest.mark.parametrize("limit", ["0", "-1", "not-a-number"])
def test_cli_runs_rejects_a_non_positive_or_invalid_limit(
    limit: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run list requires a positive integer limit."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    with pytest.raises(SystemExit) as error:
        main(["runs", "--limit", limit, "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert captured.err


def test_cli_runs_applies_a_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The run list returns only the requested number of newest runs."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["run", "--json", "--", "echo", "first"]) == 0
    capsys.readouterr()
    assert main(["run", "--json", "--", "echo", "second"]) == 0
    second_result = json.loads(capsys.readouterr().out)

    exit_code = main(["runs", "--limit", "1", "--json"])
    output = capsys.readouterr()
    result = json.loads(output.out)

    assert exit_code == 0
    assert len(result["data"]["runs"]) == 1
    assert result["data"]["runs"][0]["run_id"] == second_result["data"]["run_id"]


@pytest.mark.parametrize("command", ["show", "log", "failures"])
def test_cli_retrieval_rejects_a_run_from_another_repository(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past-run commands hide runs saved for another repository."""
    first_root = _initialise_repository(tmp_path / "first-repository")
    monkeypatch.chdir(first_root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["run", "--json", "--", "echo", "saved"]) == 0
    run_output = capsys.readouterr()
    run_id = json.loads(run_output.out)["data"]["run_id"]

    second_root = _initialise_repository(tmp_path / "second-repository")
    monkeypatch.chdir(second_root)

    exit_code = main([command, run_id, "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert result == {
        "ok": False,
        "error": {
            "code": "not-found",
            "message": f'Run "{run_id}" was not found.',
        },
    }
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
