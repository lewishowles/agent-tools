"""Run direct commands with bounded foreground execution."""

import math
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunResult:
    """Describe one completed direct command run.

    Attributes:
        argv: Argument array passed to the command process.
        working_directory: Absolute directory used by the command process.
        exit_status: Process exit status, including a negative signal status.
        timed_out: Whether the process group was terminated at the timeout.
        duration_seconds: Elapsed foreground execution time.
        output: Combined standard output and standard error.
    """

    argv: tuple[str, ...]
    working_directory: Path
    exit_status: int
    timed_out: bool
    duration_seconds: float
    output: str


def _terminate_process_group(process: subprocess.Popen[str]) -> str:
    """Terminate a process group, escalating after a two-second grace period.

    Args:
        process: Running process whose session contains the full command tree.

    Returns:
        All output collected while terminating and reaping the process group.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    try:
        output, _ = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

        output, _ = process.communicate()

    return output or ""


def run_command(argv: Sequence[str], cwd: str | Path, timeout: float) -> RunResult:
    """Run an argument-array command in the foreground.

    Args:
        argv: Non-empty argument array, including the executable name.
        cwd: Directory in which the command process starts.
        timeout: Positive number of seconds allowed before termination.

    Raises:
        ValueError: If ``argv`` is empty or ``timeout`` is not a finite positive
            number.
        OSError: If the command process cannot be started or its process group
            cannot be terminated.
        KeyboardInterrupt: After the command process group has been cleaned up.
    """
    command_arguments = tuple(argv)

    if not command_arguments:
        raise ValueError("Command arguments must contain at least one argument.")

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Command timeout must be finite and greater than zero.")

    working_directory = Path(cwd).expanduser().resolve()
    started_at = time.monotonic()
    process = subprocess.Popen(
        command_arguments,
        cwd=working_directory,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    timed_out = False

    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        output = _terminate_process_group(process)
    except KeyboardInterrupt:
        _terminate_process_group(process)
        raise

    duration_seconds = time.monotonic() - started_at

    return RunResult(
        argv=command_arguments,
        working_directory=working_directory,
        exit_status=process.returncode,
        timed_out=timed_out,
        duration_seconds=duration_seconds,
        output=output or "",
    )
