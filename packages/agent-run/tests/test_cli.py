"""Tests for the agent-run entry point and result output."""

import json
import os
import shlex
import signal
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from agent_run.cli import _format_failure_report, main
from agent_run.database import connect_database
from agent_run.execution import TerminateRequested
from agent_run.failures import Failure, FailureReport
from agent_run.locking import acquire_run_lock
from agent_run.output import render_command_result, render_error, render_success
from agent_run.repository import identify_repository
from agent_run.runs import create_run_log, start_run
from cli_style import CliStyleNotFoundError


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


def test_version_prints_the_installed_package_version(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--version` prints the installed package version and succeeds."""
    monkeypatch.setattr("agent_run.cli.version", lambda _name: "9.8.7")
    exit_code = main(["--version"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "agent-run 9.8.7\n"
    assert captured.err == ""


def test_json_version_returns_version_data(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--version --json` returns the version in a successful JSON envelope."""
    monkeypatch.setattr("agent_run.cli.version", lambda _name: "9.8.7")
    exit_code = main(["--version", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result == {
        "ok": True,
        "data": {"version": "9.8.7"},
    }
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
    assert "out\nerr\n" in text_output.out
    assert "Command completed" in text_output.out
    assert "Run ID" in text_output.out
    assert "Log path" in text_output.out
    assert (
        text_output.out.index("Command completed")
        < text_output.out.index("out\nerr\n")
        < text_output.out.index("Log path")
    )
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
    assert result["data"]["summary"] == ["out", "err"]
    log_path = Path(result["data"]["log_path"])
    assert log_path.read_text() == "out\nerr\n"
    assert json_output.err == ""


def test_cli_run_success_uses_fallback_when_log_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A successful run reports a placeholder summary when its log is unreadable."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    def unreadable_log(_path: Path) -> bytes:
        raise OSError("log unavailable")

    monkeypatch.setattr(Path, "read_bytes", unreadable_log)

    exit_code = main(["run", "--json", "--", sys.executable, "-c", "print('passed')"])
    output = capsys.readouterr()
    result = json.loads(output.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert result["data"]["summary"] == ["Output could not be read."]
    assert output.err == ""


def test_cli_show_keeps_a_long_log_path_on_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The saved log path remains copyable when it is long and contains spaces."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    log_parent = tmp_path / ("long log directory " + "x" * 80)
    log_parent.mkdir()
    database_path = log_parent / "agent run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["run", "--json", "--", sys.executable, "-c", "print('passed')"])
    run_output = capsys.readouterr()
    run_result = json.loads(run_output.out)
    run_id = run_result["data"]["run_id"]
    expected_log_path = run_result["data"]["log_path"]

    assert exit_code == 0

    show_exit_code = main(["show", run_id])
    show_output = capsys.readouterr()

    assert show_exit_code == 0
    assert f"log path: {expected_log_path}\n" in show_output.out


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


def test_cli_run_keeps_the_startup_error_when_discard_run_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cleanup failure does not replace the original startup error."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    def fail_discard_run(*_arguments: object) -> None:
        """Raise the cleanup failure that the CLI must ignore."""
        raise OSError("cleanup unavailable")

    monkeypatch.setattr("agent_run.cli.discard_run", fail_discard_run)

    exit_code = main(
        ["run", "--timeout", "5", "--json", "--", "missing-agent-run-command"]
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert result["error"]["code"] == "environment"
    assert "missing-agent-run-command" in result["error"]["message"]
    assert "cleanup unavailable" not in result["error"]["message"]
    assert "Traceback" not in captured.out
    assert captured.err == ""


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


def test_cli_manual_named_run_reports_command_without_executing_or_resolving_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A manual command prints the command to run and stops before it runs or
    resolves file targets.
    """
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    command = [sys.executable, "-c", "raise SystemExit(9)"]
    assert (
        main(
            [
                "add",
                "browser",
                "--capability",
                "file-list",
                "--manual",
                "--",
                *command,
            ]
        )
        == 0
    )
    capsys.readouterr()

    text_exit_code = main(["run", "browser", "--file", "missing.py"])
    text_output = capsys.readouterr()

    assert text_exit_code == 1
    assert text_output.out == ""
    assert "manual-only" in text_output.err
    assert str(root) in text_output.err
    assert sys.executable in text_output.err

    json_exit_code = main(["run", "browser", "--file", "missing.py", "--json"])
    json_output = capsys.readouterr()
    result = json.loads(json_output.out)

    assert json_exit_code == 1
    assert result["ok"] is False
    assert result["error"]["code"] == "manual"
    assert result["error"]["data"] == {"argv": command, "cwd": str(root)}
    assert "manual-only" in result["error"]["message"]
    assert f"cd {shlex.quote(str(root))}" in result["error"]["message"]
    assert sys.executable in result["error"]["message"]
    assert json_output.err == ""

    connection = connect_database(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        connection.close()


def test_cli_named_run_refuses_when_the_same_command_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A named run reports its active holder without creating a log or record."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    command = [sys.executable, "-c", "print('ok')"]
    assert main(["add", "build", "--", *command]) == 0
    capsys.readouterr()

    repository = identify_repository()
    active_run_id = uuid4().hex
    active_lock = acquire_run_lock(
        database_path,
        repository.id,
        "build",
        run_id=active_run_id,
    )

    try:
        exit_code = main(["run", "build", "--json"])
        output = capsys.readouterr()
        result = json.loads(output.out)

        assert exit_code == 1
        assert result["error"] == {
            "code": "busy",
            "message": (
                f'Command "build" is already running (run ID: {active_lock.run_id}).'
            ),
            "data": {"run_id": active_lock.run_id},
        }
        assert output.err == ""
        assert not (database_path.parent / "agent-run-logs").exists()

        connection = connect_database(database_path)
        try:
            assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        finally:
            connection.close()
    finally:
        active_lock.release()

    assert main(["run", "build", "--json"]) == 0
    released_output = capsys.readouterr()
    assert json.loads(released_output.out)["ok"] is True


@pytest.mark.parametrize(
    "command",
    [
        ("playwright", "test"),
        ("cypress", "run"),
        ("npx", "playwright", "test"),
        ("pnpm", "exec", "cypress", "run"),
        ("yarn", "playwright", "test"),
        ("bunx", "cypress", "run"),
        ("uv", "run", "playwright", "test"),
    ],
)
def test_cli_direct_browser_runner_is_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: tuple[str, ...],
) -> None:
    """Direct browser runners are refused before execution and logging."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["run", "--json", "--", *command])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert result["error"]["code"] == "manual"
    assert result["error"]["data"] == {"argv": list(command), "cwd": str(root)}
    expected_line = f"cd {shlex.quote(str(root))} && {shlex.join(command)}"
    assert (
        result["error"]["message"]
        == f"This command is manual-only. Run it manually with: {expected_line}"
    )
    assert captured.err == ""

    connection = connect_database(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        connection.close()


def test_cli_add_auto_marks_browser_runner_and_edit_can_remove_mark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Adding a browser runner marks it manual until the existing edit override clears it."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["add", "browser", "--", "npx", "playwright", "test"]) == 0
    add_output = capsys.readouterr()

    assert "manual             true" in add_output.out

    assert (
        main(
            [
                "edit",
                "browser",
                "--no-manual",
                "--json",
                "--",
                "npx",
                "playwright",
                "test",
            ]
        )
        == 0
    )
    edit_output = capsys.readouterr()
    result = json.loads(edit_output.out)

    assert result["data"]["manual"] is False
    assert edit_output.err == ""


def test_cli_edit_replacement_auto_marks_browser_runner_unless_overridden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Replacing a command with a browser runner marks it unless unmarked in that edit."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    assert main(["add", "browser", "--", "echo", "ok"]) == 0
    capsys.readouterr()

    assert main(["edit", "browser", "--", "playwright", "test"]) == 0
    marked_output = capsys.readouterr()

    assert "manual             true" in marked_output.out

    assert (
        main(
            [
                "edit",
                "browser",
                "--no-manual",
                "--",
                "playwright",
                "test",
            ]
        )
        == 0
    )
    overridden_output = capsys.readouterr()

    assert "manual             false" in overridden_output.out


def test_cli_detect_add_marks_browser_runner_script_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Saving a detected browser script stores the inferred manual mark."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "package.json").write_text(
        json.dumps({"scripts": {"browser": "playwright test"}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    assert main(["detect", "--add", "browser", "--json"]) == 0
    output = capsys.readouterr()

    assert output.err == ""

    connection = connect_database(database_path)
    try:
        manual = connection.execute("SELECT manual FROM commands").fetchone()[0]
    finally:
        connection.close()

    assert manual == 1


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
    assert "capability         file-list" in list_output.out

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

    def interrupt_run(*_arguments: object, **_options: object) -> None:
        connection = connect_database(tmp_path / "agent-run.db")
        try:
            row = connection.execute(
                "SELECT status, pid, duration_seconds, exit_status FROM runs"
            ).fetchone()
        finally:
            connection.close()

        assert row == ("running", os.getpid(), None, None)
        raise KeyboardInterrupt

    monkeypatch.setattr("agent_run.cli.run_command", interrupt_run)

    exit_code = main(["run", "--timeout", "5", "--", "echo"])

    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert "Error: Command interrupted." in captured.err
    assert "run ID:" in captured.err
    assert "log path:" in captured.err

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        row = connection.execute(
            "SELECT status, pid, exit_status, timed_out FROM runs"
        ).fetchone()
    finally:
        connection.close()

    assert row == ("finished", os.getpid(), -signal.SIGINT, 0)


def test_cli_run_returns_143_after_terminate_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A terminate request finalises the run and returns the SIGTERM status."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    def terminate_run(*_arguments: object, **_options: object) -> None:
        connection = connect_database(tmp_path / "agent-run.db")
        try:
            row = connection.execute(
                "SELECT status, pid, duration_seconds, exit_status FROM runs"
            ).fetchone()
        finally:
            connection.close()

        assert row == ("running", os.getpid(), None, None)
        raise TerminateRequested

    monkeypatch.setattr("agent_run.cli.run_command", terminate_run)

    exit_code = main(["run", "--timeout", "5", "--", "echo"])

    captured = capsys.readouterr()

    assert exit_code == 143
    assert captured.out == ""
    assert "Error: Command interrupted." in captured.err
    assert "run ID:" in captured.err
    assert "log path:" in captured.err

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        row = connection.execute(
            "SELECT status, pid, exit_status, timed_out FROM runs"
        ).fetchone()
    finally:
        connection.close()

    assert row == ("finished", os.getpid(), -signal.SIGTERM, 0)


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
    assert f"run ID             {run_id}" in runs_text_output.out
    assert command_path.name in runs_text_output.out

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
    assert f"run ID             {run_id}" in show_text_output.out
    assert command_path.name in show_text_output.out
    assert "exit status        1" in show_text_output.out
    assert "timeout" in show_text_output.out
    assert "5 seconds" in show_text_output.out
    assert "duration" in show_text_output.out
    assert "log path" in show_text_output.out

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


def test_cli_shows_a_running_run_without_completion_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run listings and show give the status and leave out fields that are not known yet."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    run_id, log_path = create_run_log(database_path, run_id="running-run")
    connection = connect_database(database_path)
    try:
        start_run(
            connection,
            run_id=run_id,
            repository_id=identify_repository().id,
            argv=["echo", "running"],
            working_directory=".",
            timeout_seconds=5,
            started_at="2026-09-22T09:00:00+00:00",
            log_path=log_path,
            pid=os.getpid(),
        )
        connection.commit()
    finally:
        connection.close()

    text_exit_code = main(["runs"])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert "status             running" in text_output.out
    assert "exit status" not in text_output.out
    assert "timed out" not in text_output.out
    assert "duration" not in text_output.out

    show_exit_code = main(["show", run_id])
    show_output = capsys.readouterr()

    assert show_exit_code == 0
    assert "status             running" in show_output.out
    assert "duration" not in show_output.out

    json_exit_code = main(["runs", "--json"])
    json_output = capsys.readouterr()
    json_result = json.loads(json_output.out)
    data = json_result["data"]["runs"][0]

    assert json_exit_code == 0
    assert data["run_id"] == run_id
    assert data["status"] == "running"
    assert data["exit_status"] is None
    assert data["duration_seconds"] is None
    assert "pid" not in data


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
    assert captured.out == "- No runs recorded\n"
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


def test_cli_style_text_renderer_uses_plain_fixed_width_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text renderers disable colour and use the shared fixed width."""
    options: dict[str, object] = {}

    def fake_command_result(**kwargs: object) -> str:
        options.update(kwargs)
        return "rendered"

    monkeypatch.setattr("agent_run.output.command_result", fake_command_result)
    monkeypatch.setattr(
        "agent_run.output._RENDER_OPTIONS",
        {"plain": True, "width": 80, "raise_on_missing": False},
    )

    rendered = render_command_result(
        result="success",
        summary="Command completed",
        command="echo ok",
        exit_code=0,
        duration="0.001s",
        detail="Run ID: run-1",
    )

    assert rendered == "rendered"
    assert options["plain"] is True
    assert options["width"] == 80
    assert options["raise_on_missing"] is False


def test_cli_style_text_renderer_falls_back_without_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text rendering still returns plain output when the binary is unavailable."""

    def missing_binary(binary: str) -> str:
        raise CliStyleNotFoundError(f"missing: {binary}")

    monkeypatch.setattr("cli_style.core.resolve_binary", missing_binary)

    rendered = render_command_result(
        result="success",
        summary="Command completed",
        command="echo ok",
        exit_code=0,
        duration="0.001s",
        detail="Run ID: run-1",
    )

    assert "command: echo ok" in rendered
    assert "summary: Command completed" in rendered


def test_failure_text_drops_the_repeated_source_location() -> None:
    """Failure text keeps the location in the heading only once."""
    failure = Failure(
        path="tests/example.py",
        line=4,
        column=None,
        title="AssertionError",
        detail=("E       values differ", "tests/example.py:4: AssertionError"),
    )
    report = FailureReport(
        recognised=True,
        first=failure,
        more=(),
        hidden_count=0,
        truncated=False,
        tail=(),
    )

    rendered = _format_failure_report(report)

    assert rendered.count("tests/example.py:4") == 1
    assert "E       values differ" in rendered


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
        ("busy", 1),
        ("manual", 1),
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
