"""SQLite schema and transactional migrations for progress storage."""

import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from .errors import DatabaseBusyError, MigrationFailedError, StaleSchemaError

# applies one schema version's changes to an open connection
Migration = Callable[[sqlite3.Connection], None]

# the schema version this package writes when creating a database from empty
SCHEMA_VERSION = 5
# the newest schema version this package knows how to migrate to
LATEST_SCHEMA_VERSION = SCHEMA_VERSION

# Matches a contract's step markers, `1)` or `1.` at a word boundary. The number and
# separator are captured so a split can reject a run that skips or restyles a marker.
_NUMBERED_STEP_PATTERN = re.compile(r"(?<!\S)(\d+)([.)])[ \t]+")


def utc_timestamp() -> str:
	"""Return a UTC timestamp in the public ISO 8601 format."""
	return datetime.now(UTC).isoformat()


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


def _split_contract(contract: str) -> list[str]:
	"""Split a version 1 contract into its numbered steps, or return it as a single step.

	A contract only splits when it opens on step 1 and every later marker continues that
	run with the same separator. Anything else, including a run that skips a number or
	changes separator part way through, is kept whole: an unsplit contract is obvious to
	whoever reads it next, a wrongly split one is not. A number written mid-sentence still
	splits when it happens to continue the run, because nothing here distinguishes it from
	a real step marker.
	"""
	matches = list(_NUMBERED_STEP_PATTERN.finditer(contract))
	if not matches or matches[0].start() != 0:
		return [contract]

	separator = matches[0].group(2)
	steps = []
	for position, match in enumerate(matches, start=1):
		if int(match.group(1)) != position or match.group(2) != separator:
			return [contract]

		end = matches[position].start() if position < len(matches) else len(contract)
		step = contract[match.end() : end].strip()
		if not step:
			return [contract]
		steps.append(step)

	return steps


def _split_files(files: str | None) -> list[str]:
	"""Split a version 1 semicolon-separated file list, dropping blank entries."""
	if files is None:
		return []

	return [file.strip() for file in files.split(";") if file.strip()]


def _split_acceptance_criteria(criteria: str) -> list[str]:
	"""Split newline-separated acceptance criteria, dropping blank entries."""
	return [line.strip() for line in criteria.splitlines() if line.strip()]


def _migrate_to_version_2(connection: sqlite3.Connection) -> None:
	"""Move each task's contract and files into ordered tables and add the split rationale.

	The original columns are dropped once their content has been copied across, leaving the
	normalised rows as the only source of truth. An older binary cannot read the result.
	"""
	connection.execute("ALTER TABLE tasks ADD COLUMN split_rationale TEXT")
	connection.execute(
		"""
		CREATE TABLE task_contract_steps (
			task_id TEXT NOT NULL,
			position INTEGER NOT NULL,
			text TEXT NOT NULL,
			PRIMARY KEY (task_id, position),
			FOREIGN KEY (task_id) REFERENCES tasks (id)
		)
		"""
	)
	connection.execute(
		"""
		CREATE TABLE task_files (
			task_id TEXT NOT NULL,
			position INTEGER NOT NULL,
			text TEXT NOT NULL,
			PRIMARY KEY (task_id, position),
			FOREIGN KEY (task_id) REFERENCES tasks (id)
		)
		"""
	)

	for task_id, contract, files in connection.execute(
		"SELECT id, contract, files FROM tasks"
	).fetchall():
		for position, text in enumerate(_split_contract(contract), start=1):
			connection.execute(
				"INSERT INTO task_contract_steps (task_id, position, text) VALUES (?, ?, ?)",
				(task_id, position, text),
			)

		for position, text in enumerate(_split_files(files), start=1):
			connection.execute(
				"INSERT INTO task_files (task_id, position, text) VALUES (?, ?, ?)",
				(task_id, position, text),
			)

	connection.execute("ALTER TABLE tasks DROP COLUMN contract")
	connection.execute("ALTER TABLE tasks DROP COLUMN files")


def _migrate_to_version_3(connection: sqlite3.Connection) -> None:
	"""Add release planning fields and allow notes to belong to releases.

	SQLite can't add a table-level check to an existing table, so notes is rebuilt under a
	new name, filled with every existing row, and renamed back into place.
	"""
	# Foreign keys are already on and can't be switched off inside this transaction,
	# so check them at commit instead, after the rebuilt notes table has its final name.
	connection.execute("PRAGMA defer_foreign_keys = ON")
	connection.execute("ALTER TABLE releases ADD COLUMN purpose TEXT")
	connection.execute("ALTER TABLE releases ADD COLUMN risks TEXT")
	connection.execute(
		"""
		CREATE TABLE release_out_of_scope (
			release_id TEXT NOT NULL,
			position INTEGER NOT NULL,
			text TEXT NOT NULL,
			PRIMARY KEY (release_id, position),
			FOREIGN KEY (release_id) REFERENCES releases (id)
		)
		"""
	)
	connection.execute(
		"""
		CREATE TABLE notes_new (
			id TEXT PRIMARY KEY,
			project_id TEXT NOT NULL,
			task_id TEXT,
			release_id TEXT,
			type TEXT NOT NULL CHECK (type IN ('discovery', 'decision')),
			body TEXT NOT NULL,
			supersedes_id TEXT,
			created_at TEXT NOT NULL,
			CHECK ((task_id IS NULL) != (release_id IS NULL)),
			FOREIGN KEY (project_id) REFERENCES projects (id),
			FOREIGN KEY (task_id) REFERENCES tasks (id),
			FOREIGN KEY (release_id) REFERENCES releases (id),
			FOREIGN KEY (supersedes_id) REFERENCES notes_new (id)
		)
		"""
	)
	connection.execute(
		"""
		INSERT INTO notes_new (
			id, project_id, task_id, release_id, type, body, supersedes_id, created_at
		)
		SELECT id, project_id, task_id, NULL, type, body, supersedes_id, created_at
		FROM notes
		"""
	)
	connection.execute("DROP TABLE notes")
	connection.execute("ALTER TABLE notes_new RENAME TO notes")


def _migrate_to_version_4(connection: sqlite3.Connection) -> None:
	"""Remove release purpose, risks and out-of-scope items, and the unused task model tier.

	Existing purpose text and out-of-scope items are added to the end of the
	release overview, so that text is kept. Release risks are dropped.
	The model tier column was never part of this package's schema but exists in
	some live databases, so it is dropped only when present.
	"""
	release_rows = connection.execute(
		"SELECT id, overview, purpose FROM releases"
	).fetchall()
	for release_id, overview, purpose in release_rows:
		parts = []
		if purpose:
			parts.append(purpose)

		out_of_scope = connection.execute(
			"SELECT text FROM release_out_of_scope "
			"WHERE release_id = ? ORDER BY position",
			(release_id,),
		).fetchall()
		if out_of_scope:
			parts.append(
				"Out of scope:\n" + "\n".join(f"- {row[0]}" for row in out_of_scope)
			)

		if parts:
			connection.execute(
				"UPDATE releases SET overview = ? WHERE id = ?",
				("\n\n".join((overview, *parts)), release_id),
			)

	connection.execute("DROP TABLE release_out_of_scope")
	connection.execute("ALTER TABLE releases DROP COLUMN purpose")
	connection.execute("ALTER TABLE releases DROP COLUMN risks")

	task_columns = {
		row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
	}
	if "model_tier" in task_columns:
		connection.execute("ALTER TABLE tasks DROP COLUMN model_tier")


def _migrate_to_version_5(connection: sqlite3.Connection) -> None:
	"""Merge task purpose and acceptance criteria into the remaining task fields.

	Existing purpose text is added to the end of each task overview. Each non-empty
	acceptance criterion becomes a contract step after the existing steps. Task risks
	are dropped.
	"""
	task_rows = connection.execute(
		"SELECT id, overview, purpose, acceptance_criteria FROM tasks"
	).fetchall()
	for task_id, overview, purpose, acceptance_criteria in task_rows:
		if purpose:
			connection.execute(
				"UPDATE tasks SET overview = ? WHERE id = ?",
				(f"{overview}\n\n{purpose}", task_id),
			)

		criteria = _split_acceptance_criteria(acceptance_criteria)
		if not criteria:
			continue

		position = connection.execute(
			"SELECT COALESCE(MAX(position), 0) FROM task_contract_steps WHERE task_id = ?",
			(task_id,),
		).fetchone()[0]
		for criterion in criteria:
			position += 1
			connection.execute(
				"INSERT INTO task_contract_steps (task_id, position, text) VALUES (?, ?, ?)",
				(task_id, position, criterion),
			)

	connection.execute("ALTER TABLE tasks DROP COLUMN purpose")
	connection.execute("ALTER TABLE tasks DROP COLUMN acceptance_criteria")
	connection.execute("ALTER TABLE tasks DROP COLUMN risks")


# maps each supported schema version to the migration that produces it
MIGRATIONS: dict[int, Migration] = {
	1: _create_schema,
	2: _migrate_to_version_2,
	3: _migrate_to_version_3,
	4: _migrate_to_version_4,
	5: _migrate_to_version_5,
}


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
