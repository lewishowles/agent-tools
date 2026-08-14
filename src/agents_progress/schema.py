"""SQLite schema and transactional migrations for progress storage."""

from collections.abc import Callable
from datetime import datetime, timezone
import sqlite3

from .errors import DatabaseBusyError, MigrationFailedError, StaleSchemaError

# applies one schema version's changes to an open connection
Migration = Callable[[sqlite3.Connection], None]

# the schema version this package writes when creating a database from empty
SCHEMA_VERSION = 1
# the newest schema version this package knows how to migrate to
LATEST_SCHEMA_VERSION = SCHEMA_VERSION


def utc_timestamp() -> str:
	"""Return a UTC timestamp in the public ISO 8601 format."""
	return datetime.now(timezone.utc).isoformat()


def _create_schema(connection: sqlite3.Connection) -> None:
	"""Create the version 1 tables and constraints for a fresh database."""
	# executescript commits outside a transaction, which would break migration rollback,
	# so each statement runs individually inside the caller's transaction
	statements = (
		"""
		CREATE TABLE projects (
			id TEXT PRIMARY KEY,
			slug TEXT NOT NULL,
			name TEXT NOT NULL,
			created_at TEXT NOT NULL
		)
		""",
		"""
		CREATE TABLE releases (
			id TEXT PRIMARY KEY,
			project_id TEXT NOT NULL,
			slug TEXT NOT NULL,
			title TEXT NOT NULL,
			overview TEXT NOT NULL,
			status TEXT NOT NULL CHECK (status IN ('planned', 'active', 'done')),
			position INTEGER NOT NULL,
			FOREIGN KEY (project_id) REFERENCES projects (id),
			UNIQUE (project_id, slug)
		)
		""",
		"""
		CREATE TABLE tasks (
			id TEXT PRIMARY KEY,
			project_id TEXT NOT NULL,
			slug TEXT NOT NULL,
			release_id TEXT,
			title TEXT NOT NULL,
			overview TEXT NOT NULL,
			purpose TEXT NOT NULL,
			contract TEXT NOT NULL,
			model_tier TEXT,
			files TEXT,
			acceptance_criteria TEXT NOT NULL,
			verification TEXT NOT NULL,
			risks TEXT NOT NULL,
			status TEXT NOT NULL CHECK (
				status IN ('ready', 'in-progress', 'blocked', 'needs-decision', 'done')
			),
			status_reason TEXT,
			position INTEGER NOT NULL,
			created_at TEXT NOT NULL,
			started_at TEXT,
			completed_at TEXT,
			updated_at TEXT NOT NULL,
			FOREIGN KEY (project_id) REFERENCES projects (id),
			FOREIGN KEY (release_id) REFERENCES releases (id),
			UNIQUE (project_id, slug)
		)
		""",
		"""
		CREATE TABLE task_dependencies (
			task_id TEXT NOT NULL,
			depends_on_task_id TEXT NOT NULL,
			PRIMARY KEY (task_id, depends_on_task_id),
			FOREIGN KEY (task_id) REFERENCES tasks (id),
			FOREIGN KEY (depends_on_task_id) REFERENCES tasks (id)
		)
		""",
		"""
		CREATE TABLE chunks (
			id TEXT PRIMARY KEY,
			task_id TEXT NOT NULL,
			position INTEGER NOT NULL,
			title TEXT NOT NULL,
			description TEXT NOT NULL,
			status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'done', 'skipped')),
			started_at TEXT,
			completed_at TEXT,
			FOREIGN KEY (task_id) REFERENCES tasks (id)
		)
		""",
		"""
		CREATE TABLE notes (
			id TEXT PRIMARY KEY,
			project_id TEXT NOT NULL,
			task_id TEXT,
			type TEXT NOT NULL CHECK (type IN ('discovery', 'decision')),
			body TEXT NOT NULL,
			supersedes_id TEXT,
			created_at TEXT NOT NULL,
			FOREIGN KEY (project_id) REFERENCES projects (id),
			FOREIGN KEY (task_id) REFERENCES tasks (id),
			FOREIGN KEY (supersedes_id) REFERENCES notes (id)
		)
		""",
		"""
		CREATE TABLE context (
			project_id TEXT PRIMARY KEY,
			current_goal TEXT,
			previous_step TEXT,
			next_step TEXT,
			standing_context TEXT,
			verify_with TEXT,
			stop_marker TEXT,
			updated_at TEXT NOT NULL,
			FOREIGN KEY (project_id) REFERENCES projects (id)
		)
		""",
		# enforces the one-in-progress-task-per-project rule at the database level
		"""
		CREATE UNIQUE INDEX tasks_one_in_progress_per_project
			ON tasks (project_id)
			WHERE status = 'in-progress'
		""",
		# enforces the one-active-chunk-per-task rule at the database level
		"""
		CREATE UNIQUE INDEX chunks_one_active_per_task
			ON chunks (task_id)
			WHERE status = 'active'
		""",
	)

	for statement in statements:
		connection.execute(statement)


# maps each supported schema version to the migration that produces it
MIGRATIONS: dict[int, Migration] = {1: _create_schema}


def is_busy_error(error: sqlite3.OperationalError) -> bool:
	"""Return whether a SQLite error reports the database as locked. Shared with database.py."""
	message = str(error).lower()
	return "locked" in message or "busy" in message


def _current_version(connection: sqlite3.Connection) -> int:
	"""Return the schema version already applied to a connection, or 0 when unset."""
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
	"""Apply all supported migrations or leave the prior version untouched."""
	current_version = _current_version(connection)
	if current_version > LATEST_SCHEMA_VERSION:
		raise StaleSchemaError(
			f"database schema version {current_version} is newer than supported version "
			f"{LATEST_SCHEMA_VERSION}; upgrade the progress CLI",
			{
				"database_version": current_version,
				"supported_version": LATEST_SCHEMA_VERSION,
			},
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
		current_version = _current_version(connection)

		for version in range(current_version + 1, LATEST_SCHEMA_VERSION + 1):
			migration = MIGRATIONS.get(version)
			if migration is None:
				raise MigrationFailedError(
					f"no migration is registered for schema version {version}",
					{"version": version},
				)

			migration(connection)
			connection.execute(
				"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
				(version, utc_timestamp()),
			)

		connection.commit()
	except (DatabaseBusyError, MigrationFailedError, StaleSchemaError):
		if connection.in_transaction:
			connection.rollback()
		raise
	except sqlite3.OperationalError as error:
		if connection.in_transaction:
			connection.rollback()

		if is_busy_error(error):
			raise DatabaseBusyError(
				"database remained locked for the five-second retry window",
				{"retry_after_seconds": 5},
			) from error

		raise MigrationFailedError(
			f"schema migration failed: {error}",
			{"version": current_version + 1},
		) from error
	except Exception as error:
		# MigrationFailedError is already caught above, so this branch only ever
		# sees an unexpected error and always wraps it
		if connection.in_transaction:
			connection.rollback()

		raise MigrationFailedError(
			f"schema migration failed: {error}",
			{"version": current_version + 1},
		) from error
