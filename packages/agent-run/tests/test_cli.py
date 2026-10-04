"""Tests for the agent-run entry point and result output."""

import json
import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from agent_run.cli import _format_failure_report, _format_run, main
from agent_run.database import connect_database
from agent_run.execution import RunResult, TerminateRequested
from agent_run.failures import Failure, FailureReport
from agent_run.locking import RunBusyError, acquire_run_lock
from agent_run.output import render_error, render_success
from agent_run.readers import SuccessSummary
from agent_run.repository import create_repository_id, identify_repository
from agent_run.runs import RunRecord, create_run_log, start_run


def _create_repository(path: Path) -> Path:
    """Create an empty temporary Git repository for a CLI test."""
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)

    return path


def _initialise_repository(path: Path) -> Path:
    """Create a temporary Git repository with an agent-run ID."""
    root = _create_repository(path)
    create_repository_id(root)

    return root


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


def test_init_creates_and_prints_the_repository_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Init creates and prints the ID for an uninitialised repository."""
    root = _create_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    exit_code = main(["init"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out.strip() == identify_repository().id


def test_init_prints_the_existing_repository_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Init leaves an existing repository ID unchanged."""
    root = _create_repository(tmp_path / "repository")
    existing_repository_id = create_repository_id(root)
    monkeypatch.chdir(root)

    exit_code = main(["init"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out == f"{existing_repository_id}\n"


def test_init_json_creates_and_returns_the_repository_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Init returns its newly created repository ID in the JSON envelope."""
    root = _create_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    exit_code = main(["init", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert result == {"ok": True, "data": {"id": identify_repository().id}}


def test_init_json_reports_environment_when_writing_the_id_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Init reports the environment error when Git cannot write the ID."""
    root = _create_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    git_results = [
        subprocess.CompletedProcess(["git"], 0, stdout=f"{root}\n", stderr=""),
        subprocess.CompletedProcess(["git"], 1, stdout="", stderr=""),
        subprocess.CompletedProcess(
            ["git"], 2, stdout="", stderr="fatal: could not write config"
        ),
    ]

    def run_git(
        _root: Path, _arguments: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        """Return the next simulated Git result."""
        return git_results.pop(0)

    monkeypatch.setattr("agent_run.repository._run_git", run_git)

    exit_code = main(["init", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert captured.err == ""
    assert result == {
        "ok": False,
        "error": {
            "code": "environment",
            "message": "Git could not write the local repository ID. fatal: could not write config",
        },
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["detect", "--json"],
        ["list", "--json"],
        ["run", "--json", "--", "echo"],
    ],
)
def test_commands_reject_an_uninitialised_repository_without_creating_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """Commands stop before they create data for an uninitialised repository."""
    root = _create_repository(tmp_path / "repository")
    database_path = tmp_path / "agent-run.db"
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(argv)

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert captured.err == ""
    assert not database_path.exists()
    assert result == {
        "ok": False,
        "error": {
            "code": "uninitialised",
            "message": "This repository has no agent-run ID yet. Run agent-run init here once.",
        },
    }


def test_list_keeps_a_failed_repository_id_read_as_an_environment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed Git config read is not treated as an uninitialised repository."""
    root = _create_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    git_results = [
        subprocess.CompletedProcess(["git"], 0, stdout=f"{root}\n", stderr=""),
        subprocess.CompletedProcess(
            ["git"], 2, stdout="", stderr="fatal: could not read config"
        ),
    ]

    def run_git(
        _root: Path, _arguments: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        """Return the next simulated Git result."""
        return git_results.pop(0)

    monkeypatch.setattr("agent_run.repository._run_git", run_git)

    exit_code = main(["list", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert captured.err == ""
    assert result == {
        "ok": False,
        "error": {
            "code": "environment",
            "message": "Git could not read the local repository ID. fatal: could not read config",
        },
    }


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
    assert "| out\n| err\n" in text_output.out
    assert "Command completed" in text_output.out
    assert "Run ID" in text_output.out
    assert "\nLog " in text_output.out
    assert (
        text_output.out.index("Command completed")
        < text_output.out.index("\nLog ")
        < text_output.out.index("| out\n| err\n")
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


def test_cli_run_strips_coloured_output_but_keeps_raw_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON asks the child for plain output and strips colour it still prints."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    code = (
        "import os\n"
        "print('NO_COLOR=' + os.environ.get('NO_COLOR', 'unset'))\n"
        "print('\\x1b[32mpassed\\x1b[0m')\n"
        "print('\\x1b]8;;https://example.test\\x07linked\\x1b]8;;\\x07')\n"
    )
    command = [sys.executable, "-c", code]

    assert main(["run", "--", *command]) == 0
    human_output = capsys.readouterr()
    assert "NO_COLOR=unset" in human_output.out
    assert "\x1b" not in human_output.out

    assert main(["run", "--json", "--", *command]) == 0
    json_output = json.loads(capsys.readouterr().out)
    run_data = json_output["data"]
    run_id = run_data["run_id"]
    assert run_data["summary"] == ["NO_COLOR=1", "passed", "linked"]
    assert "\x1b" in Path(run_data["log_path"]).read_text()

    assert main(["log", run_id]) == 0
    raw_log = capsys.readouterr().out
    assert "\x1b[32mpassed\x1b[0m" in raw_log

    assert main(["log", run_id, "--json"]) == 0
    plain_log = json.loads(capsys.readouterr().out)["data"]["log"]
    assert plain_log == "NO_COLOR=1\npassed\nlinked\n"


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


def test_cli_run_uses_reader_summary_for_text_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The human result and JSON data show the same selected success lines."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    class SummaryReader:
        """Supply a known summary for a successful command."""

        def matches(self, argv: Sequence[str]) -> bool:
            """Match the test command."""
            return bool(argv)

        def summarise(self, log_text: str) -> list[str]:
            """Summarise the captured output."""
            return ["Tests passed: 2 tests"]

    monkeypatch.setattr("agent_run.readers.FAILURE_READERS", (SummaryReader(),))
    command = [sys.executable, "-c", "print('raw output')"]

    assert main(["run", "--timeout", "5", "--", *command]) == 0
    text_output = capsys.readouterr()
    assert "Tests passed: 2 tests" in text_output.out
    assert "\nraw output\n" not in text_output.out

    assert main(["run", "--timeout", "5", "--json", "--", *command]) == 0
    json_output = json.loads(capsys.readouterr().out)
    assert json_output["data"]["summary"] == ["Tests passed: 2 tests"]


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


def test_cli_multiple_runs_report_every_result_and_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed saved command does not prevent later commands from running."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    for name, code in (("first", 0), ("second", 7), ("third", 0)):
        assert (
            main(["add", name, "--", sys.executable, "-c", f"raise SystemExit({code})"])
            == 0
        )
        capsys.readouterr()

    exit_code = main(["run", "first", "second", "third"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err == ""
    assert captured.out.count("Command completed") == 2
    assert "Error: Command exited with status 7." in captured.out
    assert captured.out.index("$ " + sys.executable) < captured.out.index("Summary")
    summary = captured.out.split("Summary\n", 1)[1]
    summary_lines = summary.splitlines()

    for name, status in (
        ("first", "passed"),
        ("second", "failed"),
        ("third", "passed"),
    ):
        assert any(name in line and status in line for line in summary_lines)

    assert "\n\n\n" not in captured.out
    assert "\n\nSummary\n" in captured.out

    connection = connect_database(database_path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT command_name FROM runs ORDER BY rowid"
            )
        ]
    finally:
        connection.close()

    assert names == ["first", "second", "third"]


def test_cli_multiple_runs_json_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful named commands share one successful JSON result."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    for name in ("first", "third"):
        assert main(["add", name, "--", sys.executable, "-c", "print('done')"]) == 0
        capsys.readouterr()

    success_exit_code = main(["run", "first", "third", "--json"])
    success_result = json.loads(capsys.readouterr().out)

    assert success_exit_code == 0
    assert success_result["ok"] is True

    assert [run["name"] for run in success_result["data"]["runs"]] == [
        "first",
        "third",
    ]


def test_cli_multiple_runs_text_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful named commands each print a block before the summary."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    for name in ("first", "third"):
        assert main(["add", name, "--", sys.executable, "-c", "print('done')"]) == 0
        capsys.readouterr()

    assert main(["run", "first", "third"]) == 0
    passed_output = capsys.readouterr().out
    assert passed_output.count("Command completed") == 2
    assert "\n\n\n" not in passed_output
    assert "\n\nSummary\n" in passed_output


def test_cli_multiple_runs_text_reports_failure_in_last_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed final command is separated from the earlier result and summary."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert (
        main(["add", "second", "--", sys.executable, "-c", "raise SystemExit(7)"]) == 0
    )
    capsys.readouterr()

    assert (
        main(["add", "fourth", "--", sys.executable, "-c", "raise SystemExit(8)"]) == 0
    )
    capsys.readouterr()

    assert main(["run", "second", "fourth"]) == 1
    failed_output = capsys.readouterr().out
    assert "\n\nError: Command exited with status 8." in failed_output
    assert "\n\nSummary\n" in failed_output
    assert "\n\n\n" not in failed_output


def test_cli_multiple_runs_json_includes_failure_and_timeout_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One JSON document reports every run, including failed and timed out runs."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    commands = (
        ("passed", "print('done')"),
        ("failed", "raise SystemExit(4)"),
        ("slow", "import time; time.sleep(10)"),
    )

    for name, script in commands:
        assert main(["add", name, "--", sys.executable, "-c", script]) == 0
        capsys.readouterr()

    exit_code = main(["run", "passed", "failed", "slow", "--timeout", "0.1", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    assert result["ok"] is False
    runs = result["data"]["runs"]
    assert [run["name"] for run in runs] == ["passed", "failed", "slow"]
    assert [run["status"] for run in runs] == ["passed", "failed", "timed out"]
    assert [run["ok"] for run in runs] == [True, False, False]
    assert runs[1]["data"]["exit_status"] == 4
    assert runs[1]["data"]["failure"]
    assert runs[2]["data"]["timed_out"] is True
    assert runs[2]["data"]["failure"]

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        timeouts = [
            row[0]
            for row in connection.execute(
                "SELECT timeout_seconds FROM runs ORDER BY rowid"
            )
        ]
    finally:
        connection.close()

    assert timeouts == [0.1, 0.1, 0.1]


@pytest.mark.parametrize(
    ("interruption", "expected_status"),
    [(KeyboardInterrupt, 130), (TerminateRequested, 143)],
)
def test_cli_multiple_runs_stop_after_an_interrupt(
    interruption: type[BaseException],
    expected_status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An interrupted run is saved and the later command is skipped."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    for name in ("first", "second"):
        assert main(["add", name, "--", sys.executable, "-c", "print('done')"]) == 0
        capsys.readouterr()

    def interrupt(*_args: object, **_kwargs: object) -> None:
        """Simulate a stopped child after the run record starts."""
        raise interruption

    monkeypatch.setattr("agent_run.cli.run_command", interrupt)

    exit_code = main(["run", "first", "second", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == expected_status
    assert captured.err == ""
    assert result["ok"] is False
    assert [run["name"] for run in result["data"]["runs"]] == ["first"]
    assert result["data"]["runs"][0]["status"] == "interrupted"

    text_exit_code = main(["run", "first", "second"])
    text_output = capsys.readouterr()

    assert text_exit_code == expected_status
    assert text_output.err == ""
    assert "Error: Command interrupted." in text_output.out
    assert "\n\nSummary\n" in text_output.out
    assert any(
        "first" in line and "interrupted" in line
        for line in text_output.out.split("Summary\n", 1)[1].splitlines()
    )
    assert "second" not in text_output.out

    connection = connect_database(database_path)
    try:
        records = connection.execute(
            "SELECT command_name, exit_status FROM runs ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()

    assert records == [
        ("first", 128 - expected_status),
        ("first", 128 - expected_status),
    ]


def test_cli_multiple_runs_keep_prior_results_when_a_later_run_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A busy command ends the set after reporting runs that already completed."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    for name in ("first", "second", "third"):
        assert main(["add", name, "--", sys.executable, "-c", "print('done')"]) == 0
        capsys.readouterr()

    def block_second(
        database_path: Path, repository_id: str, name: str, *, run_id: str
    ) -> object:
        """Keep the second saved name busy after the first has completed."""
        if name == "second":
            raise RunBusyError("active-run")

        return acquire_run_lock(database_path, repository_id, name, run_id=run_id)

    monkeypatch.setattr("agent_run.cli.acquire_run_lock", block_second)

    json_exit_code = main(["run", "first", "second", "third", "--json"])
    json_output = capsys.readouterr()
    result = json.loads(json_output.out)

    assert json_exit_code == 1
    assert json_output.err == ""
    assert result["ok"] is False
    runs = result["data"]["runs"]
    assert [run["name"] for run in runs] == ["first", "second"]
    assert [run["status"] for run in runs] == ["passed", "not started"]
    assert runs[1]["error"] == {
        "code": "busy",
        "message": 'Command "second" is already running (run ID: active-run).',
        "data": {"run_id": "active-run"},
    }

    text_exit_code = main(["run", "first", "second", "third"])
    text_output = capsys.readouterr()

    assert text_exit_code == 1
    assert text_output.err == ""
    assert '\n\nError: Command "second" is already running' in text_output.out
    assert "\n\nSummary\n" in text_output.out
    assert any(
        "second" in line and "not started" in line
        for line in text_output.out.split("Summary\n", 1)[1].splitlines()
    )
    assert "third" not in text_output.out

    connection = connect_database(database_path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT command_name FROM runs ORDER BY rowid"
            )
        ]
    finally:
        connection.close()

    assert names == ["first", "first"]


@pytest.mark.parametrize(
    ("failure", "error_code", "expected_exit_code"),
    [
        (ValueError("invalid command"), "usage", 2),
        (OSError("command unavailable"), "environment", 3),
    ],
)
def test_cli_multiple_runs_keep_the_exit_status_when_a_later_run_cannot_start(
    failure: Exception,
    error_code: str,
    expected_exit_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The set stops with the error status of the command that could not start."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    for name in ("first", "second", "third"):
        assert main(["add", name, "--", sys.executable, "-c", "print('done')"]) == 0
        capsys.readouterr()

    def stop_second(
        database_path: Path, repository_id: str, name: str, *, run_id: str
    ) -> object:
        """Raise the chosen startup error after the first command passes."""
        if name == "second":
            raise failure

        return acquire_run_lock(database_path, repository_id, name, run_id=run_id)

    monkeypatch.setattr("agent_run.cli.acquire_run_lock", stop_second)

    exit_code = main(["run", "first", "second", "third", "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == expected_exit_code
    assert result["ok"] is False
    assert [run["name"] for run in result["data"]["runs"]] == ["first", "second"]
    assert result["data"]["runs"][1]["status"] == "not started"
    assert result["data"]["runs"][1]["error"]["code"] == error_code


@pytest.mark.parametrize("invalid_name", ["missing", "manual"])
def test_cli_multiple_runs_validate_every_name_before_starting(
    invalid_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unknown and manual-only commands refuse the set without saving a run."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    assert main(["add", "ready", "--", sys.executable, "-c", "print('ready')"]) == 0
    assert (
        main(
            ["add", "manual", "--manual", "--", sys.executable, "-c", "print('manual')"]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = main(["run", "ready", invalid_name, "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert captured.err == ""
    assert result["ok"] is False
    assert invalid_name in result["error"]["message"]

    connection = connect_database(database_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        connection.close()

    assert count == 0


@pytest.mark.parametrize("option", ["--cwd", "--file", "--glob"])
def test_cli_multiple_runs_refuse_per_command_options(
    option: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Options that target one command cannot be applied to several names."""
    with pytest.raises(SystemExit) as error:
        main(["run", "one", "two", option, "value", "--json"])

    result = json.loads(capsys.readouterr().out)

    assert error.value.code == 2
    assert result["error"]["code"] == "usage"
    assert "several named commands" in result["error"]["message"]


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

    show_exit_code = main(["show", result["data"]["run_id"], "--json"])
    show_output = capsys.readouterr()
    show_result = json.loads(show_output.out)

    assert show_exit_code == 0
    assert show_result["data"]["command_name"] == "format"
    assert show_result["data"]["targets"] == [
        "../src/one.py",
        "../src/two.py",
        "../src/three.py",
    ]

    text_exit_code = main(["show", result["data"]["run_id"]])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert "saved command      format" in text_output.out
    assert (
        'targets            ["../src/one.py", "../src/two.py", "../src/three.py"]'
        in text_output.out
    )


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
            "print('\\x1b[31mcaptured\\x1b[0m', flush=True); raise SystemExit(9)",
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    failure = result["error"]["data"]["failure"]

    assert exit_code == 1
    assert result["ok"] is False
    assert failure["recognised"] is False
    assert failure["tail"] == ["captured"]
    assert "\x1b" not in json.dumps(result)
    assert captured.err == ""


def test_cli_recognises_coloured_vitest_failures_in_both_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failure excerpts are plain in both modes while the saved log stays raw."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    command_path = tmp_path / "vitest"
    command_path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('\\x1b[31m FAIL  tests/example.test.js > suite > first\\x1b[0m')\n"
        "print('\\x1b[31mAssertionError: broken\\x1b[0m')\n"
        "print('\\x1b[36m ❯ tests/example.test.js:4:2\\x1b[0m')\n"
        "print('\\x1b[31m FAIL  tests/example.test.js > suite > second\\x1b[0m')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    command_path.chmod(0o700)

    assert main(["run", "--", str(command_path)]) == 1
    human_output = capsys.readouterr()
    assert "suite > first" in human_output.err
    assert "AssertionError: broken" in human_output.err
    assert "\x1b" not in human_output.err

    assert main(["run", "--json", "--", str(command_path)]) == 1
    json_output = json.loads(capsys.readouterr().out)
    failure = json_output["error"]["data"]["failure"]
    run_id = json_output["error"]["data"]["run_id"]
    assert failure["recognised"] is True
    assert failure["first"]["title"] == "suite > first"
    assert failure["first"]["detail"] == ["AssertionError: broken"]
    assert failure["more"][0]["title"] == "suite > second"
    assert "\x1b" not in json.dumps(json_output)

    assert main(["failures", run_id, "--json"]) == 0
    saved_failures = json.loads(capsys.readouterr().out)
    assert saved_failures["data"]["failure"] == failure
    assert "\x1b" in Path(json_output["error"]["data"]["log_path"]).read_text()


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
    assert show_result["data"]["command_name"] == ""
    assert show_result["data"]["targets"] == []
    assert show_result["data"]["exit_status"] == 1
    assert show_result["data"]["duration_seconds"] >= 0
    assert show_result["data"]["log_path"] == run_result["error"]["data"]["log_path"]

    show_text_exit_code = main(["show", run_id])
    show_text_output = capsys.readouterr()

    assert show_text_exit_code == 0
    assert f"run ID             {run_id}" in show_text_output.out
    assert "saved command      none (direct run)" in show_text_output.out
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


def test_cli_shows_unknown_provenance_for_an_older_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show prints unknown for the saved command and targets of an older run."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    connection = connect_database(database_path)
    try:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, repository_id, argv, working_directory,
                timeout_seconds, started_at, duration_seconds, exit_status,
                timed_out, log_path, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "older-run",
                identify_repository().id,
                '["echo"]',
                ".",
                5,
                "2026-01-01T00:00:00+00:00",
                0.25,
                0,
                0,
                str(tmp_path / "older.log"),
                "finished",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    json_exit_code = main(["show", "older-run", "--json"])
    json_output = capsys.readouterr()
    data = json.loads(json_output.out)["data"]

    assert json_exit_code == 0
    assert data["command_name"] is None
    assert data["targets"] is None

    text_exit_code = main(["show", "older-run"])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert "saved command      unknown" in text_output.out
    assert "targets            unknown" in text_output.out


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
@pytest.mark.parametrize("run_id", ["", "   "])
@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_retrieval_rejects_a_blank_run_id(
    command: str,
    run_id: str,
    json_mode: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past-run commands reject an explicitly blank run ID."""
    arguments = [command, run_id]

    if json_mode:
        arguments.append("--json")

    with pytest.raises(SystemExit) as error:
        main(arguments)

    captured = capsys.readouterr()
    message = "A run ID cannot be blank. Omit it to use the latest run, or run `agent-run runs` to list saved run IDs."

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
def test_cli_retrieval_without_an_id_reads_the_latest_run(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past-run commands default to the latest run in this repository."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["run", "--json", "--", "echo", "first"]) == 0
    capsys.readouterr()
    assert main(["run", "--json", "--", "echo", "second"]) == 0
    latest_run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]

    exit_code = main([command, "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["data"]["run_id"] == latest_run_id
    assert captured.err == ""

    if command == "log":
        assert "second" in result["data"]["log"]
        assert "first" not in result["data"]["log"]


def test_cli_again_repeats_the_latest_or_a_given_direct_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct reruns keep their arguments, directory and timeout."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["run", "--cwd", "tools", "--timeout", "7", "--json", "--", "pwd"]) == 0
    first = json.loads(capsys.readouterr().out)["data"]
    assert main(["run", "--json", "--", "echo", "latest"]) == 0
    latest = json.loads(capsys.readouterr().out)["data"]

    assert main(["again", "--json"]) == 0
    repeated_latest = json.loads(capsys.readouterr().out)["data"]
    assert repeated_latest["run_id"] != latest["run_id"]
    assert repeated_latest["argv"] == ["echo", "latest"]

    assert main(["again", first["run_id"], "--json"]) == 0
    repeated_first = json.loads(capsys.readouterr().out)["data"]
    assert repeated_first["run_id"] != first["run_id"]
    assert repeated_first["argv"] == ["pwd"]
    assert repeated_first["working_directory"] == str(root / "tools")
    assert main(["show", repeated_first["run_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["timeout_seconds"] == 7


def test_cli_again_uses_the_current_saved_command_and_recorded_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A saved rerun uses the current command and the same target from any directory."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    (root / "other").mkdir()
    (root / "src").mkdir()
    (root / "src" / "one.py").write_text("one")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert (
        main(
            [
                "add",
                "check",
                "--cwd",
                "tools",
                "--timeout",
                "5",
                "--capability",
                "file-list",
                "--",
                "echo",
                "old",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(["run", "check", "--file", "src/one.py", "--timeout", "2", "--json"]) == 0
    )
    original = json.loads(capsys.readouterr().out)["data"]
    assert original["files"] == ["../src/one.py"]

    assert (
        main(["edit", "check", "--cwd", ".", "--timeout", "8", "--", "echo", "new"])
        == 0
    )
    capsys.readouterr()
    monkeypatch.chdir(root / "other")

    assert main(["again", original["run_id"], "--json"]) == 0
    repeated = json.loads(capsys.readouterr().out)["data"]
    assert repeated["run_id"] != original["run_id"]
    assert repeated["argv"] == ["echo", "new", "src/one.py"]
    assert repeated["files"] == ["src/one.py"]
    assert repeated["working_directory"] == str(root)
    assert main(["show", repeated["run_id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["timeout_seconds"] == 8


def test_cli_again_keeps_saved_command_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A saved rerun retains the lock, manual and target capability checks."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "file.py").write_text("file")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    assert main(["add", "check", "--capability", "file-list", "--", "echo"]) == 0
    capsys.readouterr()
    assert main(["run", "check", "--file", "file.py", "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]

    active_lock = acquire_run_lock(
        database_path, identify_repository().id, "check", run_id="active"
    )
    try:
        assert main(["again", run_id, "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "busy"
    finally:
        active_lock.release()

    assert main(["edit", "check", "--manual"]) == 0
    capsys.readouterr()
    assert main(["again", run_id, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "manual"

    assert main(["edit", "check", "--no-manual", "--capability", "none"]) == 0
    capsys.readouterr()
    assert main(["again", run_id, "--json"]) == 2
    assert (
        'capability "none"' in json.loads(capsys.readouterr().out)["error"]["message"]
    )

    assert main(["edit", "check", "--capability", "file-list"]) == 0
    capsys.readouterr()
    (root / "file.py").rename(root / "moved.py")
    assert main(["again", run_id, "--json"]) == 2
    assert "does not exist" in json.loads(capsys.readouterr().out)["error"]["message"]

    connection = connect_database(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        connection.close()


def test_cli_again_refuses_a_direct_browser_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A direct rerun still refuses recorded browser-runner arguments."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    assert main(["run", "--json", "--", "echo", "safe"]) == 0
    run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        connection.execute(
            "UPDATE runs SET argv = ? WHERE run_id = ?",
            ('["playwright", "test"]', run_id),
        )
        connection.commit()
    finally:
        connection.close()

    assert main(["again", run_id, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "manual"


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_again_reports_a_removed_saved_command(
    json_mode: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing saved command reports how to run its recorded arguments."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    assert main(["add", "check", "--", "echo", "ok"]) == 0
    capsys.readouterr()
    assert main(["run", "check", "--json"]) == 0
    run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]
    assert main(["remove", "check"]) == 0
    capsys.readouterr()

    arguments = ["again", run_id, "--json"] if json_mode else ["again", run_id]
    assert main(arguments) == 1
    captured = capsys.readouterr()
    message = (
        json.loads(captured.out)["error"]["message"] if json_mode else captured.err
    )
    assert 'Command "check" was not found.' in message
    assert "agent-run run -- ARGV" in message


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_again_refuses_a_run_with_unknown_provenance(
    json_mode: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run recorded before saved-command names were stored is refused."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    database_path = tmp_path / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))
    connection = connect_database(database_path)
    try:
        connection.execute(
            """
            INSERT INTO runs (
                run_id, repository_id, argv, working_directory,
                timeout_seconds, started_at, duration_seconds, exit_status,
                timed_out, log_path, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "older-run",
                identify_repository().id,
                '["echo"]',
                ".",
                5,
                "2026-01-01T00:00:00+00:00",
                0.25,
                0,
                0,
                str(tmp_path / "older.log"),
                "finished",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    arguments = (
        ["again", "older-run", "--json"] if json_mode else ["again", "older-run"]
    )
    assert main(arguments) == 2
    captured = capsys.readouterr()
    message = (
        json.loads(captured.out)["error"]["message"] if json_mode else captured.err
    )
    assert "agent-run run NAME" in message
    assert "agent-run run -- ARGV" in message


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_again_reports_no_runs(
    json_mode: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no runs in the repository, again reports the same error as show."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    assert main(["again", "--json"] if json_mode else ["again"]) == 1
    captured = capsys.readouterr()
    message = (
        json.loads(captured.out)["error"]["message"] if json_mode else captured.err
    )
    assert "No runs recorded in this repository." in message


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_again_rejects_a_blank_run_id(
    json_mode: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An explicitly blank ID is an error rather than a request for the latest run."""
    arguments = ["again", " ", "--json"] if json_mode else ["again", " "]

    with pytest.raises(SystemExit) as error:
        main(arguments)

    captured = capsys.readouterr()
    message = (
        json.loads(captured.out)["error"]["message"] if json_mode else captured.err
    )
    assert error.value.code == 2
    assert "A run ID cannot be blank." in message


@pytest.mark.parametrize("command", ["show", "log", "failures"])
def test_cli_retrieval_without_an_id_ignores_newer_runs_in_another_repository(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The latest run is chosen from the current repository only."""
    first_root = _initialise_repository(tmp_path / "first-repository")
    second_root = _initialise_repository(tmp_path / "second-repository")
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    monkeypatch.chdir(first_root)

    assert main(["run", "--json", "--", "echo", "first-repository"]) == 0
    first_run_id = json.loads(capsys.readouterr().out)["data"]["run_id"]
    monkeypatch.chdir(second_root)
    assert main(["run", "--json", "--", "echo", "second-repository"]) == 0
    capsys.readouterr()
    monkeypatch.chdir(first_root)

    exit_code = main([command, "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["data"]["run_id"] == first_run_id
    assert captured.err == ""


@pytest.mark.parametrize("command", ["show", "log", "failures"])
@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_retrieval_without_an_id_reports_no_runs(
    command: str,
    json_mode: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Past-run commands report a repository with no saved runs as not found."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main([command, "--json"] if json_mode else [command])
    captured = capsys.readouterr()
    message = "No runs recorded in this repository."

    assert exit_code == 1

    if json_mode:
        assert json.loads(captured.out) == {
            "ok": False,
            "error": {"code": "not-found", "message": message},
        }
        assert captured.err == ""
    else:
        assert captured.out == ""
        assert captured.err == f"Error: {message}\n"


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


@pytest.mark.parametrize(
    ("recognised", "heading"),
    [(True, "Summary"), (False, "Last lines of output")],
)
def test_success_block_keeps_command_and_log_on_single_lines(
    recognised: bool, heading: str
) -> None:
    """A completed run shows aligned facts before its marked output lines."""
    command = "echo " + "x" * 90
    log_path = Path("/tmp/") / ("long-log-directory-" * 5) / "run.log"
    result = RunResult(
        argv=("sh", "-c", command),
        working_directory=Path("/tmp"),
        exit_status=0,
        timed_out=False,
        duration_seconds=0.123,
        log_path=log_path,
    )
    record = RunRecord(
        run_id="run-1",
        repository_id="repo-1",
        argv=result.argv,
        working_directory=".",
        timeout_seconds=5,
        started_at="2026-10-03T12:00:00+00:00",
        duration_seconds=result.duration_seconds,
        exit_status=0,
        timed_out=False,
        log_path=log_path,
    )

    rendered = _format_run(
        result,
        record,
        SuccessSummary(lines=["first line", "second line"], recognised=recognised),
    )

    assert rendered == (
        "\n"
        "OK Command completed\n"
        f"$ {shlex.join(result.argv)}\n"
        "Exit code  0\n"
        "Duration   0.123s\n"
        "Run ID     run-1\n"
        f"Log        {log_path}\n"
        "\n"
        f"{heading}\n"
        "| first line\n"
        "| second line\n\n"
    )


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
