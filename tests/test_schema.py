import sqlite3

import pytest
from agents_progress import schema
from agents_progress.database import Database
from agents_progress.errors import MigrationFailedError, StaleSchemaError
from agents_progress.ids import (
	CHUNK_PREFIX,
	NOTE_PREFIX,
	PROJECT_PREFIX,
	RELEASE_PREFIX,
	TASK_PREFIX,
	generate_object_id,
)


def _insert_project(connection: sqlite3.Connection, project_id: str) -> None:
	connection.execute(
		"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
		(project_id, "agents", "Agent configuration", "2026-01-01T00:00:00+00:00"),
	)


def _insert_task(
	connection: sqlite3.Connection,
	project_id: str,
	task_id: str,
	slug: str,
	status: str = "ready",
) -> None:
	connection.execute(
		"""
		INSERT INTO tasks (
			id, project_id, slug, title, overview, purpose,
			acceptance_criteria, verification, risks, status, position,
			created_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			task_id,
			project_id,
			slug,
			"Task",
			"Overview",
			"Purpose",
			"Acceptance",
			"Verification",
			"Risks",
			status,
			1,
			"2026-01-01T00:00:00+00:00",
			"2026-01-01T00:00:00+00:00",
		),
	)


def _insert_legacy_task(
	connection: sqlite3.Connection,
	project_id: str,
	task_id: str,
	slug: str,
	contract: str,
	files: str | None,
) -> None:
	connection.execute(
		"""
		INSERT INTO tasks (
			id, project_id, slug, title, overview, purpose, contract, files,
			acceptance_criteria, verification, risks, status, position,
			created_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			task_id,
			project_id,
			slug,
			"Task",
			"Overview",
			"Purpose",
			contract,
			files,
			"Acceptance",
			"Verification",
			"Risks",
			"ready",
			1,
			"2026-01-01T00:00:00+00:00",
			"2026-01-01T00:00:00+00:00",
		),
	)


def _create_version_two_database(database_path) -> tuple[str, str, str, str]:
	"""Build a version 2 database with one task and two notes, the second superseding the first.

	Returns the project id, task id, first note id and second note id, in that order.
	"""
	with sqlite3.connect(database_path) as connection:
		schema._create_schema(connection)
		schema._migrate_to_version_2(connection)
		connection.execute(
			"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
		)
		connection.executemany(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
			[
				(1, "2026-01-01T00:00:00+00:00"),
				(2, "2026-01-01T00:00:00+00:00"),
			],
		)
		project_id = generate_object_id(PROJECT_PREFIX)
		task_id = generate_object_id(TASK_PREFIX)
		first_note_id = generate_object_id(NOTE_PREFIX)
		second_note_id = generate_object_id(NOTE_PREFIX)
		_insert_project(connection, project_id)
		_insert_task(connection, project_id, task_id, "task")
		connection.executemany(
			"""
			INSERT INTO notes (id, project_id, task_id, type, body, supersedes_id, created_at)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			""",
			[
				(
					first_note_id,
					project_id,
					task_id,
					"discovery",
					"First note",
					None,
					"2026-01-01T00:00:00+00:00",
				),
				(
					second_note_id,
					project_id,
					task_id,
					"discovery",
					"Replacement note",
					first_note_id,
					"2026-01-02T00:00:00+00:00",
				),
			],
		)

	return project_id, task_id, first_note_id, second_note_id


def test_first_connection_creates_the_schema_and_sqlite_safety_settings(
	tmp_path,
) -> None:
	database = Database(tmp_path / "progress.db")

	with database.connection() as connection:
		tables = {
			row[0]
			for row in connection.execute(
				"SELECT name FROM sqlite_master WHERE type = 'table'"
			)
		}

		assert {
			"projects",
			"releases",
			"tasks",
			"task_dependencies",
			"task_contract_steps",
			"task_files",
			"chunks",
			"notes",
			"context",
			"schema_migrations",
		}.issubset(tables)
		assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
		assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
		assert (
			connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
				0
			]
			== 4
		)
		release_columns = {
			row[1] for row in connection.execute("PRAGMA table_info(releases)")
		}
		assert {"purpose", "risks"}.isdisjoint(release_columns)
		assert "release_out_of_scope" not in tables
		assert "split_rationale" in {
			row[1] for row in connection.execute("PRAGMA table_info(tasks)")
		}
		task_columns = {
			row[1] for row in connection.execute("PRAGMA table_info(tasks)")
		}
		assert "contract" not in task_columns
		assert "files" not in task_columns


def test_an_older_schema_version_migrates_forward(tmp_path) -> None:
	database_path = tmp_path / "progress.db"
	with sqlite3.connect(database_path) as connection:
		connection.execute(
			"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
		)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (0, ?)",
			("2026-01-01T00:00:00+00:00",),
		)

	with Database(database_path).connection() as connection:
		assert (
			connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
				0
			]
			== 4
		)
		assert connection.execute("SELECT 1 FROM projects").fetchone() is None
		assert (
			connection.execute("SELECT 1 FROM task_contract_steps").fetchone() is None
		)
		assert connection.execute("SELECT 1 FROM task_files").fetchone() is None


def test_schema_version_two_migrates_contract_and_file_values_in_order(
	tmp_path,
) -> None:
	database_path = tmp_path / "progress.db"
	with sqlite3.connect(database_path) as connection:
		schema._create_schema(connection)
		connection.execute(
			"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
		)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
			("2026-01-01T00:00:00+00:00",),
		)
		project_id = generate_object_id(PROJECT_PREFIX)
		_insert_project(connection, project_id)
		_insert_legacy_task(
			connection,
			project_id,
			generate_object_id(TASK_PREFIX),
			"numbered",
			contract="1) First step 2) Second step\n3) Third step",
			files=" src/first.py; ;src/second.py ",
		)
		fallback_task_id = generate_object_id(TASK_PREFIX)
		_insert_legacy_task(
			connection,
			project_id,
			fallback_task_id,
			"fallback",
			contract="Intro before 1) step",
			files=None,
		)

	with Database(database_path).connection() as connection:
		numbered_task_id = connection.execute(
			"SELECT id FROM tasks WHERE slug = 'numbered'"
		).fetchone()[0]
		task_columns = {
			row[1] for row in connection.execute("PRAGMA table_info(tasks)")
		}
		assert "contract" not in task_columns
		assert "files" not in task_columns
		assert [
			tuple(row)
			for row in connection.execute(
				"SELECT position, text FROM task_contract_steps WHERE task_id = ? ORDER BY position",
				(numbered_task_id,),
			).fetchall()
		] == [(1, "First step"), (2, "Second step"), (3, "Third step")]
		assert [
			tuple(row)
			for row in connection.execute(
				"SELECT position, text FROM task_files WHERE task_id = ? ORDER BY position",
				(numbered_task_id,),
			).fetchall()
		] == [(1, "src/first.py"), (2, "src/second.py")]
		assert [
			tuple(row)
			for row in connection.execute(
				"SELECT position, text FROM task_contract_steps WHERE task_id = ? ORDER BY position",
				(fallback_task_id,),
			).fetchall()
		] == [(1, "Intro before 1) step")]
		assert (
			connection.execute(
				"SELECT 1 FROM task_files WHERE task_id = ?",
				(fallback_task_id,),
			).fetchone()
			is None
		)


def test_schema_version_two_migrates_task_notes_without_loss(tmp_path) -> None:
	database_path = tmp_path / "progress.db"
	project_id, task_id, first_note_id, second_note_id = _create_version_two_database(
		database_path
	)

	with Database(database_path).connection() as connection:
		assert (
			connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
				0
			]
			== 4
		)
		assert [
			tuple(row)
			for row in connection.execute(
				"SELECT id, project_id, task_id, release_id, body, supersedes_id "
				"FROM notes ORDER BY created_at"
			).fetchall()
		] == [
			(first_note_id, project_id, task_id, None, "First note", None),
			(
				second_note_id,
				project_id,
				task_id,
				None,
				"Replacement note",
				first_note_id,
			),
		]


def test_schema_version_three_moves_release_fields_and_drops_legacy_model_tier(
	tmp_path,
) -> None:
	database_path = tmp_path / "progress.db"
	with sqlite3.connect(database_path) as connection:
		schema._create_schema(connection)
		schema._migrate_to_version_2(connection)
		schema._migrate_to_version_3(connection)
		connection.execute(
			"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
		)
		connection.executemany(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
			[
				(1, "2026-01-01T00:00:00+00:00"),
				(2, "2026-01-01T00:00:00+00:00"),
				(3, "2026-01-01T00:00:00+00:00"),
			],
		)
		project_id = generate_object_id(PROJECT_PREFIX)
		release_id = generate_object_id(RELEASE_PREFIX)
		unchanged_release_id = generate_object_id(RELEASE_PREFIX)
		_insert_project(connection, project_id)
		connection.executemany(
			"""
			INSERT INTO releases (
				id, project_id, slug, title, overview, purpose, risks, status, position
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			[
				(
					release_id,
					project_id,
					"release",
					"Release",
					"Release overview.",
					"Release purpose.",
					"Release risks.",
					"planned",
					1,
				),
				(
					unchanged_release_id,
					project_id,
					"unchanged",
					"Unchanged release",
					"Unchanged overview.",
					None,
					"Dropped risks.",
					"planned",
					2,
				),
			],
		)
		connection.executemany(
			"INSERT INTO release_out_of_scope (release_id, position, text) VALUES (?, ?, ?)",
			[
				(release_id, 1, "First excluded item."),
				(release_id, 2, "Second excluded item."),
			],
		)
		connection.execute("ALTER TABLE tasks ADD COLUMN model_tier TEXT")
		_insert_task(connection, project_id, generate_object_id(TASK_PREFIX), "task")
		connection.execute(
			"UPDATE tasks SET model_tier = ? WHERE project_id = ?",
			("legacy", project_id),
		)

	with Database(database_path).connection() as connection:
		assert (
			connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
				0
			]
			== 4
		)
		assert connection.execute(
			"SELECT overview FROM releases WHERE id = ?", (release_id,)
		).fetchone()[0] == (
			"Release overview.\n\nRelease purpose.\n\nOut of scope:\n"
			"- First excluded item.\n- Second excluded item."
		)
		assert (
			connection.execute(
				"SELECT overview FROM releases WHERE id = ?", (unchanged_release_id,)
			).fetchone()[0]
			== "Unchanged overview."
		)
		release_columns = {
			row[1] for row in connection.execute("PRAGMA table_info(releases)")
		}
		task_columns = {
			row[1] for row in connection.execute("PRAGMA table_info(tasks)")
		}
		assert {"purpose", "risks"}.isdisjoint(release_columns)
		assert "model_tier" not in task_columns
		assert (
			connection.execute(
				"SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
				("release_out_of_scope",),
			).fetchone()
			is None
		)


def test_notes_require_exactly_one_owner(tmp_path) -> None:
	with Database(tmp_path / "progress.db").connection() as connection:
		project_id = generate_object_id(PROJECT_PREFIX)
		task_id = generate_object_id(TASK_PREFIX)
		release_id = generate_object_id(RELEASE_PREFIX)
		_insert_project(connection, project_id)
		_insert_task(connection, project_id, task_id, "task")
		connection.execute(
			"""
			INSERT INTO releases (id, project_id, slug, title, overview, status, position)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			""",
			(release_id, project_id, "release", "Release", "Overview", "planned", 1),
		)

		with pytest.raises(sqlite3.IntegrityError):
			connection.execute(
				"""
				INSERT INTO notes (
					id, project_id, task_id, release_id, type, body, created_at
				) VALUES (?, ?, ?, ?, ?, ?, ?)
				""",
				(
					generate_object_id(NOTE_PREFIX),
					project_id,
					task_id,
					release_id,
					"discovery",
					"Both owners",
					"2026-01-01T00:00:00+00:00",
				),
			)

		with pytest.raises(sqlite3.IntegrityError):
			connection.execute(
				"""
				INSERT INTO notes (
					id, project_id, type, body, created_at
				) VALUES (?, ?, ?, ?, ?)
				""",
				(
					generate_object_id(NOTE_PREFIX),
					project_id,
					"discovery",
					"Neither owner",
					"2026-01-01T00:00:00+00:00",
				),
			)


def test_failed_version_three_migration_rolls_back_to_version_two(
	tmp_path, monkeypatch
) -> None:
	database_path = tmp_path / "progress.db"
	_create_version_two_database(database_path)

	def broken_migration(connection: sqlite3.Connection) -> None:
		connection.execute("ALTER TABLE releases ADD COLUMN temporary TEXT")
		raise RuntimeError("deliberate version 3 migration failure")

	monkeypatch.setitem(schema.MIGRATIONS, 3, broken_migration)

	with pytest.raises(
		MigrationFailedError, match="deliberate version 3 migration failure"
	):
		Database(database_path).connect()

	with sqlite3.connect(database_path) as connection:
		assert (
			connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
				0
			]
			== 2
		)
		assert "temporary" not in {
			row[1] for row in connection.execute("PRAGMA table_info(releases)")
		}


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch) -> None:
	def broken_migration(connection: sqlite3.Connection) -> None:
		connection.execute("CREATE TABLE should_rollback (value TEXT)")
		raise RuntimeError("deliberate migration failure")

	monkeypatch.setitem(schema.MIGRATIONS, 1, broken_migration)

	with pytest.raises(MigrationFailedError, match="deliberate migration failure"):
		Database(tmp_path / "progress.db").connect()

	with sqlite3.connect(tmp_path / "progress.db") as connection:
		tables = {
			row[0]
			for row in connection.execute(
				"SELECT name FROM sqlite_master WHERE type = 'table'"
			)
		}
		assert "schema_migrations" not in tables
		assert "should_rollback" not in tables


def test_newer_schema_refuses_without_changing_the_version(tmp_path) -> None:
	database_path = tmp_path / "progress.db"
	with sqlite3.connect(database_path) as connection:
		connection.execute(
			"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
		)
		connection.execute(
			"INSERT INTO schema_migrations (version, applied_at) VALUES (99, ?)",
			("2026-01-01T00:00:00+00:00",),
		)

	with pytest.raises(StaleSchemaError, match="newer"):
		Database(database_path).connect()

	with sqlite3.connect(database_path) as connection:
		assert (
			connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
				0
			]
			== 99
		)


def test_foreign_keys_and_uniqueness_constraints_protect_records(tmp_path) -> None:
	with Database(tmp_path / "progress.db").connection() as connection:
		project_id = generate_object_id(PROJECT_PREFIX)
		second_project_id = generate_object_id(PROJECT_PREFIX)
		release_id = generate_object_id(RELEASE_PREFIX)
		task_id = generate_object_id(TASK_PREFIX)
		second_task_id = generate_object_id(TASK_PREFIX)
		chunk_id = generate_object_id(CHUNK_PREFIX)

		_insert_project(connection, project_id)
		_insert_project(connection, second_project_id)
		connection.execute(
			"""
			INSERT INTO releases (id, project_id, slug, title, overview, status, position)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			""",
			(release_id, project_id, "release", "Release", "Overview", "planned", 1),
		)
		with pytest.raises(sqlite3.IntegrityError):
			connection.execute(
				"""
				INSERT INTO releases (id, project_id, slug, title, overview, status, position)
				VALUES (?, ?, ?, ?, ?, ?, ?)
				""",
				(
					generate_object_id(RELEASE_PREFIX),
					project_id,
					"release",
					"Duplicate",
					"Overview",
					"planned",
					2,
				),
			)

		_insert_task(connection, project_id, task_id, "task", "in-progress")
		with pytest.raises(sqlite3.IntegrityError):
			_insert_task(
				connection, project_id, second_task_id, "second", "in-progress"
			)

		connection.execute(
			"""
			INSERT INTO chunks (id, task_id, position, title, description, status)
			VALUES (?, ?, ?, ?, ?, ?)
			""",
			(chunk_id, task_id, 1, "Chunk", "Description", "active"),
		)
		with pytest.raises(sqlite3.IntegrityError):
			connection.execute(
				"""
				INSERT INTO chunks (id, task_id, position, title, description, status)
				VALUES (?, ?, ?, ?, ?, ?)
				""",
				(
					generate_object_id(CHUNK_PREFIX),
					task_id,
					2,
					"Second",
					"Description",
					"active",
				),
			)

		with pytest.raises(sqlite3.IntegrityError):
			connection.execute(
				"INSERT INTO releases (id, project_id, slug, title, overview, status, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
				(
					generate_object_id(RELEASE_PREFIX),
					"missing-project",
					"missing",
					"Missing",
					"Overview",
					"planned",
					1,
				),
			)
