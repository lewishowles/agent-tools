"""Tests for private logs and immutable run records."""

import os
from pathlib import Path

import pytest
from agent_run.database import connect_database
from agent_run.runs import (
    RunNotFoundError,
    create_run_log,
    get_run,
    list_runs,
    resolve_log_directory,
    save_run,
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


def test_save_run_inserts_and_reads_all_record_fields(tmp_path: Path) -> None:
    """A saved run can be read back without losing its stored fields."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)
    run_id, log_path = create_run_log(database_path)

    try:
        record = save_run(
            connection,
            run_id=run_id,
            repository_id="repository-id",
            argv=("pytest", "tests"),
            working_directory="tools",
            timeout_seconds=5,
            started_at="2026-09-16T12:00:00+00:00",
            duration_seconds=0.25,
            exit_status=4,
            timed_out=False,
            log_path=log_path,
        )
        stored_record = get_run(connection, run_id)
    finally:
        connection.close()

    assert stored_record == record


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
        save_run(
            connection,
            run_id=first_id,
            repository_id="repository-id",
            argv=("first",),
            working_directory=".",
            timeout_seconds=5,
            started_at="2026-09-16T12:00:00+00:00",
            duration_seconds=0.25,
            exit_status=0,
            timed_out=False,
            log_path=first_log_path,
        )
        save_run(
            connection,
            run_id=second_id,
            repository_id="repository-id",
            argv=("second",),
            working_directory=".",
            timeout_seconds=5,
            started_at="2026-09-16T13:00:00+00:00",
            duration_seconds=0.5,
            exit_status=1,
            timed_out=False,
            log_path=second_log_path,
        )
        save_run(
            connection,
            run_id=other_id,
            repository_id="other-repository-id",
            argv=("other",),
            working_directory=".",
            timeout_seconds=5,
            started_at="2026-09-16T14:00:00+00:00",
            duration_seconds=0.75,
            exit_status=0,
            timed_out=False,
            log_path=other_log_path,
        )

        records = list_runs(connection, "repository-id")
        limited_records = list_runs(connection, "repository-id", limit=1)
    finally:
        connection.close()

    assert [record.run_id for record in records] == [second_id, first_id]
    assert [record.run_id for record in limited_records] == [second_id]
