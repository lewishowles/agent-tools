"""Tests for the agent-run SQLite database."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from agent_run import database, schema
from agent_run.database import connect_database, resolve_database_path


def test_environment_override_selects_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment override selects the database when no path is passed."""
    database_path = tmp_path / "configured" / "agent-run.db"
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    assert resolve_database_path() == database_path

    connection = connect_database()
    connection.close()

    assert database_path.exists()


def test_database_file_has_owner_only_permissions(tmp_path: Path) -> None:
    """A newly created database file is readable and writable only by its owner."""
    database_path = tmp_path / "nested" / "agent-run.db"

    connection = connect_database(database_path)
    connection.close()

    permissions = os.stat(database_path).st_mode & 0o777

    assert permissions == 0o600


def test_database_migrations_are_idempotent(tmp_path: Path) -> None:
    """Opening the same database again keeps one applied migration record."""
    database_path = tmp_path / "agent-run.db"

    connection = connect_database(database_path)
    try:
        assert schema.current_version(connection) == schema.LATEST_SCHEMA_VERSION
    finally:
        connection.close()

    connection = connect_database(database_path)
    try:
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        connection.close()

    assert [row[0] for row in rows] == list(range(1, schema.LATEST_SCHEMA_VERSION + 1))


def test_commands_table_is_created_by_second_migration(tmp_path: Path) -> None:
    """The command migration creates the repository-scoped command table."""
    database_path = tmp_path / "agent-run.db"

    connection = connect_database(database_path)
    try:
        columns = connection.execute("PRAGMA table_info(commands)").fetchall()
    finally:
        connection.close()

    assert [column[1] for column in columns] == [
        "repository_id",
        "name",
        "argv",
        "working_directory",
        "created_at",
        "timeout_seconds",
        "capability",
        "manual",
    ]


def test_existing_database_gains_new_command_and_run_fields(tmp_path: Path) -> None:
    """Opening an existing database migrates command and run records."""
    database_path = tmp_path / "agent-run.db"

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE commands (
                repository_id TEXT NOT NULL,
                name TEXT NOT NULL,
                argv TEXT NOT NULL,
                working_directory TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (repository_id, name)
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                repository_id TEXT NOT NULL,
                argv TEXT NOT NULL,
                working_directory TEXT NOT NULL,
                timeout_seconds REAL NOT NULL,
                started_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                exit_status INTEGER NOT NULL,
                timed_out INTEGER NOT NULL,
                log_path TEXT NOT NULL
            );
            INSERT INTO runs (
                run_id, repository_id, argv, working_directory,
                timeout_seconds, started_at, duration_seconds, exit_status,
                timed_out, log_path
            ) VALUES (
                'legacy-run', 'repository-id', '["echo"]', '.', 5,
                '2026-01-01T00:00:00+00:00', 0.25, 0, 0, '/tmp/legacy.log'
            );
            INSERT INTO schema_migrations (version, applied_at)
            VALUES
                (1, '2026-01-01T00:00:00+00:00'),
                (2, '2026-01-01T00:00:00+00:00'),
                (3, '2026-01-01T00:00:00+00:00');
            INSERT INTO commands (
                repository_id, name, argv, working_directory, created_at
            ) VALUES ('repository-id', 'test', '["echo"]', '.', '2026-01-01');
            """
        )

    connection = connect_database(database_path)
    try:
        timeout, manual = connection.execute(
            "SELECT timeout_seconds, manual FROM commands WHERE name = 'test'"
        ).fetchone()
        status, pid = connection.execute(
            "SELECT status, pid FROM runs WHERE run_id = 'legacy-run'"
        ).fetchone()
    finally:
        connection.close()

    assert timeout is None
    assert manual == 0
    assert status == "finished"
    assert pid is None


def test_runs_table_is_created_by_third_migration(tmp_path: Path) -> None:
    """The run migration creates all fields needed for saved records."""
    database_path = tmp_path / "agent-run.db"

    connection = connect_database(database_path)
    try:
        columns = connection.execute("PRAGMA table_info(runs)").fetchall()
    finally:
        connection.close()

    assert [column[1] for column in columns] == [
        "run_id",
        "repository_id",
        "argv",
        "working_directory",
        "timeout_seconds",
        "started_at",
        "duration_seconds",
        "exit_status",
        "timed_out",
        "log_path",
        "status",
        "pid",
    ]


def test_newer_schema_version_raises_and_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database from a newer release raises and closes its connection."""
    database_path = tmp_path / "agent-run.db"

    connection = connect_database(database_path)
    connection.close()

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (schema.LATEST_SCHEMA_VERSION + 1, "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()

    opened_connections: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def track_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        """Open a real connection and record it so the test can check it was closed."""
        connection = real_connect(*args, **kwargs)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(database.sqlite3, "connect", track_connection)

    with pytest.raises(schema.NewerSchemaError, match="newer than supported"):
        connect_database(database_path)

    assert len(opened_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened_connections[0].execute("SELECT 1")


def test_failed_migration_rolls_back_all_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing migration leaves no migration table or partial schema behind."""
    database_path = tmp_path / "agent-run.db"

    def fail_migration(connection: sqlite3.Connection) -> None:
        """Create a table, then fail so the test can check it was rolled back."""
        connection.execute("CREATE TABLE partial_schema (id INTEGER PRIMARY KEY)")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(schema, "MIGRATIONS", (fail_migration,))

    with pytest.raises(RuntimeError, match="migration failed"):
        connect_database(database_path)

    with closing(sqlite3.connect(database_path)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

    assert tables == []
