from pathlib import Path

import pytest

from agents_progress.database import Database
from agents_progress.errors import NotFoundError, WrongObjectIdTypeError
from agents_progress.projects import Project
from agents_progress.reads import ReadStore


PROJECT_ID = "prj_" + "p" * 22
RELEASE_A = "rel_" + "a" * 22
TASK_A = "tsk_" + "a" * 22
TASK_B = "tsk_" + "b" * 22
CHUNK_A = "chk_" + "a" * 22
DISCOVERY_A = "nte_" + "a" * 22
DISCOVERY_B = "nte_" + "b" * 22
DECISION_A = "nte_" + "c" * 22


class _ProjectStore:
	"""Stand in for ProjectStore, returning the seeded test project with no Git repository."""

	def __init__(self, database: Database) -> None:
		self.database = database

	def current(self, path: str | Path | None = None) -> Project:
		return Project(
			PROJECT_ID, "agents", "Agent configuration", "2026-01-01T00:00:00+00:00"
		)


def _seed_store(tmp_path: Path) -> ReadStore:
	database = Database(tmp_path / "progress.db")
	with database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(PROJECT_ID, "agents", "Agent configuration", "2026-01-01T00:00:00+00:00"),
		)
		connection.execute(
			"INSERT INTO releases (id, project_id, slug, title, overview, status, position) "
			"VALUES (?, ?, ?, ?, ?, ?, ?)",
			(
				RELEASE_A,
				PROJECT_ID,
				"progress-store",
				"Progress store",
				"Store project progress.",
				"active",
				1,
			),
		)
		for task_id, slug, title, status, position in (
			(TASK_B, "second", "Second task", "ready", 2),
			(TASK_A, "first", "First task", "in-progress", 1),
		):
			connection.execute(
				"INSERT INTO tasks ("
				"id, project_id, slug, release_id, title, overview, purpose, contract, "
				"model_tier, files, acceptance_criteria, verification, risks, status, "
				"status_reason, position, created_at, started_at, completed_at, updated_at"
				") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
				(
					task_id,
					PROJECT_ID,
					slug,
					RELEASE_A,
					title,
					f"Overview for {slug}.",
					f"Purpose for {slug}.",
					f"Contract for {slug}.",
					"sonnet",
					None,
					"Acceptance criteria.",
					"Verification.",
					"Risks.",
					status,
					None,
					position,
					"2026-01-01T00:00:00+00:00",
					"2026-01-01T00:00:00+00:00" if status == "in-progress" else None,
					None,
					"2026-01-01T00:00:00+00:00",
				),
			)
		connection.execute(
			"INSERT INTO chunks (id, task_id, position, title, description, status, started_at, completed_at) "
			"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
			(
				CHUNK_A,
				TASK_A,
				1,
				"Read surface",
				"Implement read queries.",
				"active",
				"2026-01-01T00:00:00+00:00",
				None,
			),
		)
		for note_id, task_id, note_type, body, created_at in (
			(
				DISCOVERY_B,
				TASK_B,
				"discovery",
				"Second discovery.",
				"2026-01-01T00:00:02+00:00",
			),
			(
				DISCOVERY_A,
				TASK_A,
				"discovery",
				"First discovery.",
				"2026-01-01T00:00:01+00:00",
			),
			(
				DECISION_A,
				TASK_A,
				"decision",
				"First decision.",
				"2026-01-01T00:00:03+00:00",
			),
		):
			connection.execute(
				"INSERT INTO notes (id, project_id, task_id, type, body, supersedes_id, created_at) "
				"VALUES (?, ?, ?, ?, ?, ?, ?)",
				(note_id, PROJECT_ID, task_id, note_type, body, None, created_at),
			)

	return ReadStore(database, _ProjectStore(database))


def test_next_returns_the_task_chunk_and_next_command(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	result = store.next()

	assert result["project"] == {
		"id": PROJECT_ID,
		"slug": "agents",
		"name": "Agent configuration",
	}
	assert result["task"]["id"] == TASK_A
	assert result["chunk"]["id"] == CHUNK_A
	assert result["hint_command"] == f"progress chunk complete {CHUNK_A}"


def test_task_list_and_ready_use_position_then_object_id_and_pagination(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	result = store.task_list(limit=1)
	ready = store.ready()

	assert [item["id"] for item in result["items"]] == [TASK_A]
	assert result["has_more"] is True
	assert [item["id"] for item in ready["items"]] == [TASK_B]


def test_ready_excludes_unfinished_dependencies_and_includes_done_dependencies(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
			(TASK_B, TASK_A),
		)

	assert store.ready()["items"] == []

	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE tasks SET status = 'done' WHERE id = ?",
			(TASK_A,),
		)

	assert [item["id"] for item in store.ready()["items"]] == [TASK_B]


def test_task_get_rejects_a_wrong_object_type_before_lookup(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(WrongObjectIdTypeError):
		store.task_get(CHUNK_A)


def test_release_and_chunk_get_return_the_full_current_project_records(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	release = store.release_get(RELEASE_A)
	chunk = store.chunk_get(CHUNK_A)

	assert release["id"] == RELEASE_A
	assert release["title"] == "Progress store"
	assert chunk["id"] == CHUNK_A
	assert chunk["task_id"] == TASK_A


@pytest.mark.parametrize(
	("method_name", "object_id"),
	[("release_get", "rel_" + "r" * 22), ("chunk_get", "chk_" + "c" * 22)],
)
def test_get_raises_not_found_for_an_unknown_record(
	tmp_path: Path, method_name: str, object_id: str
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(NotFoundError):
		getattr(store, method_name)(object_id)


def test_note_lists_filter_type_and_optional_task_in_creation_order(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	discoveries = store.discovery_list()
	task_discoveries = store.discovery_list(TASK_A)
	decisions = store.decision_list(TASK_A)
	empty = store.decision_list(TASK_B)

	assert [item["id"] for item in discoveries["items"]] == [DISCOVERY_A, DISCOVERY_B]
	assert [item["type"] for item in discoveries["items"]] == [
		"discovery",
		"discovery",
	]
	assert [item["id"] for item in task_discoveries["items"]] == [DISCOVERY_A]
	assert [item["id"] for item in decisions["items"]] == [DECISION_A]
	assert empty["items"] == []
	assert empty["has_more"] is False


def test_context_get_returns_a_not_set_result_without_a_context_row(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	assert store.context_get() == {"status": "not-set", "project_id": PROJECT_ID}


def test_context_get_returns_the_current_project_context(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO context ("
			"project_id, current_goal, previous_step, next_step, standing_context, "
			"verify_with, stop_marker, updated_at"
			") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
			(
				PROJECT_ID,
				"Finish reads",
				"Inspect queries",
				"Run tests",
				"Keep changes narrow",
				"test:unit",
				"Stop after verification",
				"2026-01-01T00:00:04+00:00",
			),
		)

	result = store.context_get()

	assert result == {
		"project_id": PROJECT_ID,
		"current_goal": "Finish reads",
		"previous_step": "Inspect queries",
		"next_step": "Run tests",
		"standing_context": "Keep changes narrow",
		"verify_with": "test:unit",
		"stop_marker": "Stop after verification",
		"updated_at": "2026-01-01T00:00:04+00:00",
	}
