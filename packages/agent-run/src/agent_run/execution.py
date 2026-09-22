"""Run direct commands with bounded foreground execution."""

import math
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# Timeout used when a direct or named run has no configured override.
DEFAULT_TIMEOUT_SECONDS = 120


class TerminateRequested(Exception):
    """Raised inside ``run_command`` when agent-run receives SIGTERM.

    Any child that had started has already been stopped by the time this
    reaches the caller, which is left to finish the run record and exit.
    """


@dataclass(frozen=True)
class RunResult:
    """Describe one completed direct command run.

    Attributes:
        argv: Argument array passed to the command process.
        working_directory: Absolute directory used by the command process.
        exit_status: Process exit status, including a negative signal status.
        timed_out: Whether the process group was terminated at the timeout.
        duration_seconds: Elapsed foreground execution time.
        log_path: Private file containing combined standard output and error.
    """

    argv: tuple[str, ...]
    working_directory: Path
    exit_status: int
    timed_out: bool
    duration_seconds: float
    log_path: Path


def _open_log_file(log_path: Path) -> BinaryIO:
    """Open a run log so the command can write its output straight to the file.

    Appends rather than truncates, and keeps the file readable only by its owner.
    """
    descriptor = os.open(
        log_path,
        os.O_CREAT | os.O_APPEND | os.O_WRONLY,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "ab")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Stop a command and everything it started, then reap the process.

    Sends SIGTERM to the whole group and SIGKILL if it is still running after
    two seconds.

    Args:
        process: Running process whose session contains the full command tree.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

        process.communicate()


def run_command(
    argv: Sequence[str], cwd: str | Path, timeout: float, log_path: str | Path
) -> RunResult:
    """Run an argument-array command in the foreground.

    Args:
        argv: Non-empty argument array, including the executable name.
        cwd: Directory in which the command process starts.
        timeout: Positive number of seconds allowed before termination.
        log_path: File that receives combined standard output and standard error.

    Raises:
        ValueError: If ``argv`` is empty or ``timeout`` is not a finite positive
            number.
        OSError: If the command process cannot be started or its process group
            cannot be terminated.
        KeyboardInterrupt: After the command process group has been cleaned up.
        TerminateRequested: After the command process group has been cleaned up.
    """
    command_arguments = tuple(argv)

    if not command_arguments:
        raise ValueError("Command arguments must contain at least one argument.")

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Command timeout must be finite and greater than zero.")

    working_directory = Path(cwd).expanduser().resolve()
    resolved_log_path = Path(log_path).expanduser().resolve()
    started_at = time.monotonic()
    timed_out = False

    with _open_log_file(resolved_log_path) as log_file:

        def handle_terminate(_signum: int, _frame: object) -> None:
            """Turn SIGTERM into ``TerminateRequested`` so the caller can stop
            the child and record the run as finished.
            """
            raise TerminateRequested

        # The handler applies while the child starts and runs.
        previous_handler = signal.signal(signal.SIGTERM, handle_terminate)
        try:
            process = subprocess.Popen(
                command_arguments,
                cwd=working_directory,
                start_new_session=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )

            try:
                process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(process)
            except (KeyboardInterrupt, TerminateRequested):
                _terminate_process_group(process)
                raise
        finally:
            # Restore the previous handler after the child finishes so SIGTERM
            # behaves as before this run.
            signal.signal(signal.SIGTERM, previous_handler)

    duration_seconds = time.monotonic() - started_at

    return RunResult(
        argv=command_arguments,
        working_directory=working_directory,
        exit_status=process.returncode,
        timed_out=timed_out,
        duration_seconds=duration_seconds,
        log_path=resolved_log_path,
    )
