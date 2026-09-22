"""Tests for direct foreground command execution."""

import signal
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from agent_run import execution
from agent_run.execution import TerminateRequested, run_command
from agent_run.runs import create_run_log


def _create_log(tmp_path: Path) -> Path:
    """Create a private log path for a direct execution test."""
    _, log_path = create_run_log(tmp_path / "agent-run.db")
    return log_path


def test_run_command_writes_combined_output_and_success_status(
    tmp_path: Path,
) -> None:
    """A successful command writes output to its log and returns zero status."""
    log_path = _create_log(tmp_path)
    previous_handler = signal.getsignal(signal.SIGTERM)
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; print('out', flush=True); print('err', file=sys.stderr)",
        ],
        tmp_path,
        timeout=5,
        log_path=log_path,
    )

    assert result.argv == (
        sys.executable,
        "-c",
        "import sys; print('out', flush=True); print('err', file=sys.stderr)",
    )
    assert result.working_directory == tmp_path.resolve()
    assert result.exit_status == 0
    assert result.timed_out is False
    assert result.duration_seconds >= 0
    assert result.log_path == log_path
    assert log_path.read_text() == "out\nerr\n"
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_run_command_preserves_non_zero_exit_status(tmp_path: Path) -> None:
    """A failed command writes output and preserves its exit status."""
    log_path = _create_log(tmp_path)
    result = run_command(
        [sys.executable, "-c", "print('failed'); raise SystemExit(4)"],
        tmp_path,
        timeout=5,
        log_path=log_path,
    )

    assert result.exit_status == 4
    assert result.timed_out is False
    assert log_path.read_text() == "failed\n"


def test_run_command_terminates_a_grandchild_on_timeout(tmp_path: Path) -> None:
    """A timeout terminates the command and its grandchild process group."""
    log_path = _create_log(tmp_path)
    marker = tmp_path / "grandchild-alive"
    grandchild_code = (
        "import pathlib, sys, time; "
        "time.sleep(0.5); pathlib.Path(sys.argv[1]).write_text('alive')"
    )
    command_code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "time.sleep(10)"
    )

    result = run_command(
        [
            sys.executable,
            "-c",
            command_code,
            grandchild_code,
            str(marker),
        ],
        tmp_path,
        timeout=0.1,
        log_path=log_path,
    )

    assert result.timed_out is True
    assert result.exit_status < 0
    assert not marker.exists()


def test_run_command_keeps_output_written_before_timeout(tmp_path: Path) -> None:
    """A timed-out command keeps output written before process cleanup."""
    log_path = _create_log(tmp_path)
    result = run_command(
        [
            sys.executable,
            "-c",
            "print('before timeout', flush=True); import time; time.sleep(10)",
        ],
        tmp_path,
        timeout=0.1,
        log_path=log_path,
    )

    assert result.timed_out is True
    assert log_path.read_text() == "before timeout\n"


def test_run_command_cleans_up_before_propagating_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt cleans up the process before it reaches the caller."""
    log_path = _create_log(tmp_path)
    process = Mock()
    process.pid = 123
    process.communicate.side_effect = KeyboardInterrupt
    cleanup = Mock()

    monkeypatch.setattr(execution.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(execution, "_terminate_process_group", cleanup)

    with pytest.raises(KeyboardInterrupt):
        run_command(["command"], tmp_path, timeout=5, log_path=log_path)

    cleanup.assert_called_once_with(process)


def test_run_command_cleans_up_before_propagating_terminate_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminate request cleans up the process before it reaches the caller."""
    log_path = _create_log(tmp_path)
    previous_handler = signal.getsignal(signal.SIGTERM)
    process = Mock()
    process.pid = 123
    process.communicate.side_effect = TerminateRequested
    cleanup = Mock()

    monkeypatch.setattr(execution.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(execution, "_terminate_process_group", cleanup)

    with pytest.raises(TerminateRequested):
        run_command(["command"], tmp_path, timeout=5, log_path=log_path)

    assert signal.getsignal(signal.SIGTERM) == previous_handler
    cleanup.assert_called_once_with(process)


def test_run_command_applies_working_directory(tmp_path: Path) -> None:
    """The child process starts in the requested working directory."""
    log_path = _create_log(tmp_path)
    working_directory = tmp_path / "working"
    working_directory.mkdir()

    result = run_command(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        working_directory,
        timeout=5,
        log_path=log_path,
    )

    assert result.working_directory == working_directory.resolve()
    assert log_path.read_text() == f"{working_directory.resolve()}\n"


@pytest.mark.parametrize(
    ("argv", "timeout", "message"),
    [
        ([], 5, "at least one argument"),
        (["command"], 0, "greater than zero"),
    ],
)
def test_run_command_rejects_invalid_inputs(
    argv: list[str], timeout: float, message: str, tmp_path: Path
) -> None:
    """Invalid command arguments and timeouts fail before starting a process."""
    with pytest.raises(ValueError, match=message):
        run_command(argv, tmp_path, timeout, tmp_path / "run.log")


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_run_command_rejects_non_finite_timeout(timeout: float, tmp_path: Path) -> None:
    """A non-finite timeout fails before starting a process."""
    with pytest.raises(ValueError, match="finite and greater than zero"):
        run_command(["command"], tmp_path, timeout, tmp_path / "run.log")
