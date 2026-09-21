"""Tests for private logs and command run records."""

import os
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from agent_run.database import connect_database
from agent_run.runs import (
    RUNNING_STATUS,
    RunNotFoundError,
    create_run_log,
    finalise_run,
    get_run,
    list_runs,
    resolve_log_directory,
    start_run,
)


def test_create_run_log_uses_private_directory_beside_database(
    tmp_path: Path,
) -> None:
    """Run logs use an owner-only directory and file beside the database."""
    database_path = tmp_path / "configured" / "agent-run.db"

    run_id, log_path = create_run_log(database_path)

    assert log_path.parent == resolve_log_directory(database_path)
    assert log_path.name == f"{run_id}.log"
    assert os.stat(log_path.parent).st_mode & 0o777 == 0o700
    assert os.stat(log_path).st_mode & 0o777 == 0o600


def test_create_run_log_uses_an_allocated_run_id(tmp_path: Path) -> None:
    """A caller can use one allocated ID for both a lock and its log."""
    database_path = tmp_path / "agent-run.db"
    allocated_run_id = uuid4().hex

    run_id, log_path = create_run_log(database_path, run_id=allocated_run_id)

    assert run_id == allocated_run_id
    assert log_path.name == f"{allocated_run_id}.log"


def test_start_run_inserts_and_reads_all_record_fields(tmp_path: Path) -> None:
    """A saved run can be read back without losing its stored fields."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)
    run_id, log_path = create_run_log(database_path)

    try:
        record = start_run(
            connection,
            run_id=run_id,
            repository_id="repository-id",
            argv=("pytest", "tests"),
            working_directory="tools",
            timeout_seconds=5,
            started_at="2026-09-16T12:00:00+00:00",
            log_path=log_path,
            pid=1234,
        )
        stored_record = get_run(connection, run_id)
    finally:
        connection.close()

    assert stored_record == record


def test_finalise_run_updates_a_running_record(tmp_path: Path) -> None:
    """Finalising a running record stores its outcome and keeps its process ID."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)
    run_id, log_path = create_run_log(database_path)

    try:
        running_record = start_run(
            connection,
            run_id=run_id,
            repository_id="repository-id",
            argv=("pytest", "tests"),
            working_directory="tools",
            timeout_seconds=5,
            started_at="2026-09-16T12:00:00+00:00",
            log_path=log_path,
            pid=1234,
        )
        finished_record = finalise_run(
            connection,
            run_id=run_id,
            duration_seconds=0.25,
            exit_status=4,
            timed_out=False,
        )
    finally:
        connection.close()

    assert running_record.status == "running"
    assert running_record.duration_seconds is None
    assert running_record.exit_status is None
    assert finished_record.status == "finished"
    assert finished_record.pid == 1234
    assert finished_record.duration_seconds == 0.25
    assert finished_record.exit_status == 4


def test_finalise_run_failure_keeps_the_running_record_and_log(
    tmp_path: Path,
) -> None:
    """A failed finalisation leaves the running record and its log in place."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)
    run_id, log_path = create_run_log(database_path)

    start_run(
        connection,
        run_id=run_id,
        repository_id="repository-id",
        argv=("pytest", "tests"),
        working_directory="tools",
        timeout_seconds=5,
        started_at="2026-09-16T12:00:00+00:00",
        log_path=log_path,
        pid=1234,
    )
    connection.close()

    with pytest.raises(sqlite3.Error):
        finalise_run(
            connection,
            run_id=run_id,
            duration_seconds=0.25,
            exit_status=4,
            timed_out=False,
        )

    connection = connect_database(database_path)
    try:
        stored_record = get_run(connection, run_id)
    finally:
        connection.close()

    assert stored_record.status == RUNNING_STATUS
    assert stored_record.duration_seconds is None
    assert stored_record.exit_status is None
    assert log_path.exists()


def test_get_run_rejects_an_unknown_id(tmp_path: Path) -> None:
    """Reading an unknown run ID raises a not-found error."""
    connection = connect_database(tmp_path / "agent-run.db")

    try:
        with pytest.raises(RunNotFoundError, match="was not found"):
            get_run(connection, "missing-run")
    finally:
        connection.close()


def test_list_runs_returns_one_repository_newest_first(tmp_path: Path) -> None:
    """Run listing is limited to the repository and ordered newest first."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)
    first_id, first_log_path = create_run_log(database_path)
    second_id, second_log_path = create_run_log(database_path)
    other_id, other_log_path = create_run_log(database_path)

    try:
        start_run(
            connection,
            run_id=first_id,
            repository_id="repository-id",
            argv=("first",),
            working_directory=".",
            timeout_seconds=5,
            started_at="2026-09-16T12:00:00+00:00",
            log_path=first_log_path,
            pid=1234,
        )
        start_run(
            connection,
            run_id=second_id,
            repository_id="repository-id",
            argv=("second",),
            working_directory=".",
            timeout_seconds=5,
            started_at="2026-09-16T13:00:00+00:00",
            log_path=second_log_path,
            pid=1234,
        )
        start_run(
            connection,
            run_id=other_id,
            repository_id="other-repository-id",
            argv=("other",),
            working_directory=".",
            timeout_seconds=5,
            started_at="2026-09-16T14:00:00+00:00",
            log_path=other_log_path,
            pid=1234,
        )

        records = list_runs(connection, "repository-id")
        limited_records = list_runs(connection, "repository-id", limit=1)
    finally:
        connection.close()

    assert [record.run_id for record in records] == [second_id, first_id]
    assert [record.run_id for record in limited_records] == [second_id]
