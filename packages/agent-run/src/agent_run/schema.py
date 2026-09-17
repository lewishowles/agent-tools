"""Create and migrate the SQLite schema used by agent-run."""

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

# Applies one schema version to an open database connection.
Migration = Callable[[sqlite3.Connection], None]


class NewerSchemaError(RuntimeError):
    """Report a database schema version newer than this release supports."""


def _create_schema_migrations(connection: sqlite3.Connection) -> None:
    """Create the table that records which migrations have been applied."""
    connection.execute(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def _create_commands(connection: sqlite3.Connection) -> None:
    """Create the table that stores named commands for each repository."""
    connection.execute(
        """
        CREATE TABLE commands (
            repository_id TEXT NOT NULL,
            name TEXT NOT NULL,
            argv TEXT NOT NULL,
            working_directory TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (repository_id, name)
        )
        """
    )


def _create_runs(connection: sqlite3.Connection) -> None:
    """Create the table that stores immutable command run records."""
    connection.execute(
        """
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
        )
        """
    )


# Migrations in the order they run. A migration's version is its position,
# starting at 1, so add new migrations to the end and never reorder them.
MIGRATIONS: tuple[Migration, ...] = (
    _create_schema_migrations,
    _create_commands,
    _create_runs,
)
# Newest schema version this release understands.
LATEST_SCHEMA_VERSION = len(MIGRATIONS)


def current_version(connection: sqlite3.Connection) -> int:
    """Return the latest applied schema version, or zero for a new database."""
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table is None:
        return 0

    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    return int(row[0])


def migrate(connection: sqlite3.Connection) -> None:
    """Bring the database up to the latest schema, one transaction per migration.

    Raises NewerSchemaError when the database is newer than this release, so an
    older agent-run never writes to a schema it does not understand. A failed
    migration is rolled back and not recorded.
    """
    version = current_version(connection)
    if version > LATEST_SCHEMA_VERSION:
        raise NewerSchemaError(
            f"database schema version {version} is newer than supported version "
            f"{LATEST_SCHEMA_VERSION}"
        )

    for next_version in range(version + 1, LATEST_SCHEMA_VERSION + 1):
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Another agent-run process may have applied this migration while this process waited for the lock.
            version = current_version(connection)
            if version >= next_version:
                connection.commit()
                continue

            migration = MIGRATIONS[next_version - 1]
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (next_version, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
