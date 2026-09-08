import sqlite3

import pytest
from agents_progress import schema
from agents_progress.database import Database
from agents_progress.errors import MigrationFailedError, StaleSchemaError
from agents_progress.ids import (
	CHUNK_PREFIX,
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
			== 2
		)
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
			== 2
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
