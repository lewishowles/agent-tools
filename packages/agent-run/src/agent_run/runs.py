"""Create private run logs and store immutable run records."""

import json
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agent_run.database import resolve_database_path

# Folder beside the database that holds one log file per run.
LOG_DIRECTORY_NAME = "agent-run-logs"

# Number of recent runs shown when no listing limit is supplied.
DEFAULT_RUN_LIMIT = 20


class RunNotFoundError(LookupError):
    """Raised when no saved run has the requested ID."""


@dataclass(frozen=True)
class RunRecord:
    """One finished command run, as stored in the database.

    Attributes:
        run_id: Random ID that names the run and its log file.
        repository_id: ID of the repository the command ran in.
        argv: Argument array that was run.
        working_directory: Directory relative to the repository root.
        timeout_seconds: Time allowed before the command was stopped.
        started_at: UTC start time in ISO 8601 format.
        duration_seconds: Time the command ran for.
        exit_status: Exit status, negative when a signal ended the command.
        timed_out: Whether the command was stopped at its timeout.
        log_path: File holding the command's combined output.
    """

    run_id: str
    repository_id: str
    argv: tuple[str, ...]
    working_directory: str
    timeout_seconds: float
    started_at: str
    duration_seconds: float
    exit_status: int
    timed_out: bool
    log_path: Path


def resolve_log_directory(database_path: str | Path | None = None) -> Path:
    """Return the private log directory beside the selected database."""
    return (
        resolve_database_path(database_path).expanduser().resolve().parent
        / LOG_DIRECTORY_NAME
    )


def create_run_log(database_path: str | Path | None = None) -> tuple[str, Path]:
    """Create an empty log for a new run and return the run ID and log path.

    The log folder and file are readable only by their owner. The folder's
    permissions are reset each time because mkdir ignores ``mode`` for a folder
    that already exists.
    """
    log_directory = resolve_log_directory(database_path)
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_directory, 0o700)

    run_id = uuid4().hex
    log_path = log_directory / f"{run_id}.log"
    descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)

    return run_id, log_path


def discard_run_log(log_path: str | Path) -> None:
    """Remove the log of a command that never started. A log with output is kept."""
    resolved_log_path = Path(log_path).expanduser().resolve()

    if resolved_log_path.exists() and resolved_log_path.stat().st_size == 0:
        resolved_log_path.unlink()


def save_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    repository_id: str,
    argv: Sequence[str],
    working_directory: str,
    timeout_seconds: float,
    started_at: str,
    duration_seconds: float,
    exit_status: int,
    timed_out: bool,
    log_path: str | Path,
) -> RunRecord:
    """Save a finished run and return its record.

    Runs are only ever added, never changed, so the caller saves each run once,
    after the command has ended.
    """
    command_arguments = tuple(argv)
    resolved_log_path = Path(log_path).expanduser().resolve()
    record = RunRecord(
        run_id=run_id,
        repository_id=repository_id,
        argv=command_arguments,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
        started_at=started_at,
        duration_seconds=duration_seconds,
        exit_status=exit_status,
        timed_out=timed_out,
        log_path=resolved_log_path,
    )

    connection.execute(
        """
        INSERT INTO runs (
            run_id,
            repository_id,
            argv,
            working_directory,
            timeout_seconds,
            started_at,
            duration_seconds,
            exit_status,
            timed_out,
            log_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.run_id,
            record.repository_id,
            json.dumps(record.argv),
            record.working_directory,
            record.timeout_seconds,
            record.started_at,
            record.duration_seconds,
            record.exit_status,
            int(record.timed_out),
            str(record.log_path),
        ),
    )

    return record


def _run_from_row(row: sqlite3.Row | tuple[object, ...]) -> RunRecord:
    """Decode one database row into a run record."""
    (
        run_id,
        repository_id,
        argv,
        working_directory,
        timeout_seconds,
        started_at,
        duration_seconds,
        exit_status,
        timed_out,
        log_path,
    ) = row

    return RunRecord(
        run_id=str(run_id),
        repository_id=str(repository_id),
        argv=tuple(json.loads(str(argv))),
        working_directory=str(working_directory),
        timeout_seconds=float(timeout_seconds),
        started_at=str(started_at),
        duration_seconds=float(duration_seconds),
        exit_status=int(exit_status),
        timed_out=bool(timed_out),
        log_path=Path(str(log_path)),
    )


def get_run(connection: sqlite3.Connection, run_id: str) -> RunRecord:
    """Return one stored run by ID or raise a not-found error."""
    row = connection.execute(
        """
        SELECT
            run_id,
            repository_id,
            argv,
            working_directory,
            timeout_seconds,
            started_at,
            duration_seconds,
            exit_status,
            timed_out,
            log_path
        FROM runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    if row is None:
        raise RunNotFoundError(f'Run "{run_id}" was not found.')

    return _run_from_row(row)


def list_runs(
    connection: sqlite3.Connection,
    repository_id: str,
    limit: int = DEFAULT_RUN_LIMIT,
) -> tuple[RunRecord, ...]:
    """Return up to ``limit`` saved runs for one repository, newest first."""
    if limit <= 0:
        raise ValueError("Run limit must be a positive integer.")

    rows = connection.execute(
        """
        SELECT
            run_id,
            repository_id,
            argv,
            working_directory,
            timeout_seconds,
            started_at,
            duration_seconds,
            exit_status,
            timed_out,
            log_path
        FROM runs
        WHERE repository_id = ?
        ORDER BY started_at DESC, rowid DESC
        LIMIT ?
        """,
        (repository_id, limit),
    ).fetchall()

    return tuple(_run_from_row(row) for row in rows)
