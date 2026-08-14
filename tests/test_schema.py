import sqlite3

import pytest

from agents_progress.database import Database
from agents_progress.errors import MigrationFailedError, StaleSchemaError
from agents_progress.ids import (
	CHUNK_PREFIX,
	PROJECT_PREFIX,
	RELEASE_PREFIX,
	TASK_PREFIX,
	generate_object_id,
)
from agents_progress import schema


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
			id, project_id, slug, title, overview, purpose, contract,
			acceptance_criteria, verification, risks, status, position,
			created_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			task_id,
			project_id,
			slug,
			"Task",
			"Overview",
			"Purpose",
			"Contract",
			"Acceptance",
			"Verification",
			"Risks",
			status,
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
			== 1
		)


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
			== 1
		)
		assert connection.execute("SELECT 1 FROM projects").fetchone() is None


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
