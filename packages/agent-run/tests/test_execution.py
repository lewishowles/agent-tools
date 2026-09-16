"""Tests for direct foreground command execution."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from agent_run import execution
from agent_run.execution import run_command


def test_run_command_captures_combined_output_and_success_status(
    tmp_path: Path,
) -> None:
    """A successful command returns its output and zero exit status."""
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; print('out', flush=True); print('err', file=sys.stderr)",
        ],
        tmp_path,
        timeout=5,
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
    assert result.output == "out\nerr\n"


def test_run_command_preserves_non_zero_exit_status(tmp_path: Path) -> None:
    """A failed command returns its exit status and captured output."""
    result = run_command(
        [sys.executable, "-c", "print('failed'); raise SystemExit(4)"],
        tmp_path,
        timeout=5,
    )

    assert result.exit_status == 4
    assert result.timed_out is False
    assert result.output == "failed\n"


def test_run_command_terminates_a_grandchild_on_timeout(tmp_path: Path) -> None:
    """A timeout terminates the command and its grandchild process group."""
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
    )

    assert result.timed_out is True
    assert result.exit_status < 0
    assert not marker.exists()


def test_run_command_cleans_up_before_propagating_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt cleans up the process before it reaches the caller."""
    process = Mock()
    process.pid = 123
    process.communicate.side_effect = KeyboardInterrupt
    cleanup = Mock()

    monkeypatch.setattr(execution.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(execution, "_terminate_process_group", cleanup)

    with pytest.raises(KeyboardInterrupt):
        run_command(["command"], tmp_path, timeout=5)

    cleanup.assert_called_once_with(process)


def test_run_command_applies_working_directory(tmp_path: Path) -> None:
    """The child process starts in the requested working directory."""
    working_directory = tmp_path / "working"
    working_directory.mkdir()

    result = run_command(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        working_directory,
        timeout=5,
    )

    assert result.output == f"{working_directory.resolve()}\n"


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
        run_command(argv, tmp_path, timeout)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_run_command_rejects_non_finite_timeout(timeout: float, tmp_path: Path) -> None:
    """A non-finite timeout fails before starting a process."""
    with pytest.raises(ValueError, match="finite and greater than zero"):
        run_command(["command"], tmp_path, timeout)
