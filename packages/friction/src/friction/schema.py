"""Create and migrate the SQLite schema used by friction."""

from collections.abc import Callable
from datetime import datetime, timezone
import sqlite3

# Applies one schema version to an open database connection.
Migration = Callable[[sqlite3.Connection], None]
# Records the schema version created by this package release.
SCHEMA_VERSION = 2
# Defines the newest schema version this package can open.
LATEST_SCHEMA_VERSION = SCHEMA_VERSION


def is_busy_error(error: sqlite3.OperationalError) -> bool:
	"""Return whether SQLite reported a busy or locked database."""
	message = str(error).lower()
	return "locked" in message or "busy" in message


def utc_timestamp() -> str:
	"""Return the current UTC time in ISO 8601 form."""
	return datetime.now(timezone.utc).isoformat()


def _create_schema(connection: sqlite3.Connection) -> None:
	"""Create the tables required to store events, resolutions, and imports."""
	statements = (
		"""
		CREATE TABLE events (
			id INTEGER PRIMARY KEY,
			timestamp_utc TEXT NOT NULL,
			category TEXT NOT NULL,
			cwd TEXT NOT NULL,
			detail TEXT NOT NULL,
			source TEXT NOT NULL CHECK (source IN ('manual', 'claude-hook')),
			tool_name TEXT,
			discriminator TEXT,
			error TEXT
		)
		""",
		"""
		CREATE TABLE resolutions (
			id INTEGER PRIMARY KEY,
			category TEXT NOT NULL,
			pattern TEXT NOT NULL,
			resolved_at_utc TEXT NOT NULL,
			reference TEXT NOT NULL
		)
		""",
		"""
		CREATE TABLE import_provenance (
			source_path TEXT NOT NULL,
			source_line INTEGER NOT NULL,
			imported_at_utc TEXT NOT NULL,
			UNIQUE (source_path, source_line)
		)
		""",
	)

	for statement in statements:
		connection.execute(statement)


def _drop_runtime(connection: sqlite3.Connection) -> None:
	"""Recreate events without the runtime value that source already identifies."""
	columns = {
		row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()
	}
	if "runtime" not in columns:
		return

	connection.execute("ALTER TABLE events RENAME TO events_with_required_runtime")
	connection.execute(
		"""
		CREATE TABLE events (
			id INTEGER PRIMARY KEY,
			timestamp_utc TEXT NOT NULL,
			category TEXT NOT NULL,
			cwd TEXT NOT NULL,
			detail TEXT NOT NULL,
			source TEXT NOT NULL CHECK (source IN ('manual', 'claude-hook')),
			tool_name TEXT,
			discriminator TEXT,
			error TEXT
		)
		"""
	)
	connection.execute(
		"""
		INSERT INTO events (
			id, timestamp_utc, category, cwd, detail, source,
			tool_name, discriminator, error
		)
		SELECT id, timestamp_utc, category, cwd, detail, source,
			tool_name, discriminator, error
		FROM events_with_required_runtime
		"""
	)
	connection.execute("DROP TABLE events_with_required_runtime")


# Maps each version to the migration that creates it.
MIGRATIONS: dict[int, Migration] = {
	1: _create_schema,
	2: _drop_runtime,
}


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
	"""Apply each missing schema migration in one immediate transaction."""
	version = current_version(connection)
	if version > LATEST_SCHEMA_VERSION:
		raise RuntimeError(
			f"database schema version {version} is newer than supported version "
			f"{LATEST_SCHEMA_VERSION}"
		)

	try:
		connection.execute("BEGIN IMMEDIATE")
		connection.execute(
			"""
			CREATE TABLE IF NOT EXISTS schema_migrations (
				version INTEGER PRIMARY KEY,
				applied_at TEXT NOT NULL
			)
			"""
		)
		version = current_version(connection)

		for next_version in range(version + 1, LATEST_SCHEMA_VERSION + 1):
			migration = MIGRATIONS[next_version]
			migration(connection)
			connection.execute(
				"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
				(next_version, utc_timestamp()),
			)

		connection.commit()
	except Exception:
		if connection.in_transaction:
			connection.rollback()
		raise
