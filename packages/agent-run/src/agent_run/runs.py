"""Create private run logs and store command run records."""

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
# Status of a run that has not finished yet.
RUNNING_STATUS = "running"
# Status of a run whose command has finished.
FINISHED_STATUS = "finished"


class RunNotFoundError(LookupError):
    """Raised when no saved run has the requested ID."""


@dataclass(frozen=True)
class RunRecord:
    """One command run, as stored in the database.

    Attributes:
        run_id: Random ID that names the run and its log file.
        repository_id: ID of the repository the command ran in.
        argv: Argument array that was run.
        working_directory: Directory relative to the repository root.
        timeout_seconds: Time allowed before the command was stopped.
        started_at: UTC start time in ISO 8601 format.
        duration_seconds: Time the command ran for, or ``None`` while running.
        exit_status: Exit status, negative when a signal ended the command, or
            ``None`` while running.
        timed_out: Whether the command was stopped at its timeout.
        log_path: File holding the command's combined output.
        status: Whether the command is still running or has finished.
        pid: Process ID of the agent-run process that owns the record.
    """

    run_id: str
    repository_id: str
    argv: tuple[str, ...]
    working_directory: str
    timeout_seconds: float
    started_at: str
    duration_seconds: float | None
    exit_status: int | None
    timed_out: bool
    log_path: Path
    status: str = FINISHED_STATUS
    pid: int | None = None


def resolve_log_directory(database_path: str | Path | None = None) -> Path:
    """Return the private log directory beside the selected database."""
    return (
        resolve_database_path(database_path).expanduser().resolve().parent
        / LOG_DIRECTORY_NAME
    )


def create_run_log(
    database_path: str | Path | None = None,
    *,
    run_id: str | None = None,
) -> tuple[str, Path]:
    """Create an empty log for a new run and return the run ID and log path.

    Args:
        database_path: Database whose sibling directory stores the log.
        run_id: An ID chosen earlier so the run's lock and log share it. A new
            ID is generated when this is omitted.

    The log folder and file are readable only by their owner. The folder's
    permissions are reset each time because mkdir ignores ``mode`` for a folder
    that already exists.
    """
    log_directory = resolve_log_directory(database_path)
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_directory, 0o700)

    run_id = uuid4().hex if run_id is None else run_id
    log_path = log_directory / f"{run_id}.log"
    descriptor = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)

    return run_id, log_path


def discard_run_log(log_path: str | Path) -> None:
    """Remove the log of a command that never started. A log with output is kept."""
    resolved_log_path = Path(log_path).expanduser().resolve()

    if resolved_log_path.exists() and resolved_log_path.stat().st_size == 0:
        resolved_log_path.unlink()


def discard_run(
    connection: sqlite3.Connection,
    run_id: str,
    log_path: str | Path,
) -> None:
    """Remove a run record and its empty log after the command did not start."""
    connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    discard_run_log(log_path)


def start_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    repository_id: str,
    argv: Sequence[str],
    working_directory: str,
    timeout_seconds: float,
    started_at: str,
    log_path: str | Path,
    pid: int,
) -> RunRecord:
    """Save a new running record, which ``finalise_run`` completes."""
    command_arguments = tuple(argv)
    resolved_log_path = Path(log_path).expanduser().resolve()
    record = RunRecord(
        run_id=run_id,
        repository_id=repository_id,
        argv=command_arguments,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
        started_at=started_at,
        duration_seconds=None,
        exit_status=None,
        timed_out=False,
        log_path=resolved_log_path,
        status=RUNNING_STATUS,
        pid=pid,
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
            log_path,
            status,
            pid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            record.status,
            record.pid,
        ),
    )

    return record


def finalise_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    duration_seconds: float,
    exit_status: int,
    timed_out: bool,
) -> RunRecord:
    """Mark a running record finished with the command's final outcome."""
    connection.execute(
        """
        UPDATE runs
        SET
            duration_seconds = ?,
            exit_status = ?,
            timed_out = ?,
            status = ?
        WHERE run_id = ?
        """,
        (duration_seconds, exit_status, int(timed_out), FINISHED_STATUS, run_id),
    )

    return get_run(connection, run_id)


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
        status,
        pid,
    ) = row

    return RunRecord(
        run_id=str(run_id),
        repository_id=str(repository_id),
        argv=tuple(json.loads(str(argv))),
        working_directory=str(working_directory),
        timeout_seconds=float(timeout_seconds),
        started_at=str(started_at),
        duration_seconds=(
            None if duration_seconds is None else float(duration_seconds)
        ),
        exit_status=None if exit_status is None else int(exit_status),
        timed_out=bool(timed_out),
        log_path=Path(str(log_path)),
        status=str(status),
        pid=None if pid is None else int(pid),
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
            log_path,
            status,
            pid
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
            log_path,
            status,
            pid
        FROM runs
        WHERE repository_id = ?
        ORDER BY started_at DESC, rowid DESC
        LIMIT ?
        """,
        (repository_id, limit),
    ).fetchall()

    return tuple(_run_from_row(row) for row in rows)


def list_all_runs(connection: sqlite3.Connection) -> tuple[RunRecord, ...]:
    """Return every saved run across repositories, oldest first."""
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
            log_path,
            status,
            pid
        FROM runs
        ORDER BY started_at ASC, rowid ASC
        """
    ).fetchall()

    return tuple(_run_from_row(row) for row in rows)
