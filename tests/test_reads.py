from pathlib import Path

import pytest

from agents_progress.database import Database
from agents_progress.errors import NotFoundError, WrongObjectIdTypeError
from agents_progress.projects import Project
from agents_progress.reads import ReadStore
from agents_progress.writes import WriteStore


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


def test_next_returns_the_earliest_ready_task_when_nothing_is_in_progress(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	first_ready = writer.task_add(
		"first-ready", "First ready", release_id=RELEASE_A, position=0
	)

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_A,))

	result = store.next()

	assert result["task"]["id"] == first_ready["id"]
	assert result["task"]["status"] == "ready"
	assert result["chunk"] is None
	assert result["hint_command"] == f"progress task start {first_ready['id']}"


def test_next_prefers_active_release_over_lower_position_planned_release(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	planned_release = writer.release_add(
		"planned", "Planned", status="planned", position=0
	)
	writer.task_add(
		"planned-task",
		"Planned task",
		release_id=planned_release["id"],
		position=0,
	)

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_A,))

	result = store.next()

	assert result["task"]["id"] == TASK_B
	assert result["task"]["release_id"] == RELEASE_A


def test_next_reports_the_earliest_blocked_task_without_changing_it(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	blocked = writer.task_add(
		"blocked", "Blocked", release_id=RELEASE_A, depends_on=[TASK_A], position=3
	)

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_A,))
		connection.execute("UPDATE tasks SET position = 6 WHERE id = ?", (TASK_B,))

	before = store.task_get(blocked["id"])

	result = store.next()
	after = store.task_get(blocked["id"])

	assert result["task"]["id"] == blocked["id"]
	assert result["task"]["status"] == "blocked"
	assert result["task"]["status_reason"] == before["status_reason"]
	assert result["dependency_ids"] == [TASK_A]
	assert after["status"] == before["status"]
	assert after["status_reason"] == before["status_reason"]
	assert after["updated_at"] == before["updated_at"]


def test_next_keeps_a_manually_blocked_task_without_dependencies_blocked(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	writer.task_block(TASK_B, "Waiting for a decision")

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_A,))

	result = store.next()
	task = store.task_get(TASK_B)

	assert result["task"]["id"] == TASK_B
	assert result["task"]["status"] == "blocked"
	assert result["task"]["status_reason"] == "Waiting for a decision"
	assert result["dependency_ids"] == []
	assert result["chunk"] is None
	assert result["hint_command"] == f"progress task unblock {TASK_B}"
	assert task["status"] == "blocked"
	assert task["status_reason"] == "Waiting for a decision"


def test_current_stays_in_progress_only_when_ready_tasks_exist(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_A,))

	result = store.current()

	assert result["task"] is None
	assert result["chunk"] is None
	assert result["hint_command"] == "progress ready"


def test_next_points_to_task_list_when_no_actionable_task_exists(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done'", ())

	result = store.next()

	assert result["task"] is None
	assert result["chunk"] is None
	assert result["hint_command"] == "progress task list"


def test_task_list_and_ready_use_position_then_object_id_and_pagination(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	result = store.task_list(limit=1)
	ready = store.ready()

	assert [item["id"] for item in result["items"]] == [TASK_A]
	assert result["has_more"] is True
	assert [item["id"] for item in ready["items"]] == [TASK_B]


def test_doctor_reports_blank_required_fields_across_all_pages(
	tmp_path: Path, monkeypatch
) -> None:
	store = _seed_store(tmp_path)
	release_offsets: list[int] = []
	task_offsets: list[int] = []
	release_pages = [
		{
			"items": [
				{
					"id": "rel_" + "b" * 22,
					"title": "Blank release",
					"overview": "",
				}
			],
			"limit": 1,
			"offset": 0,
			"has_more": True,
		},
		{
			"items": [],
			"limit": 1,
			"offset": 1,
			"has_more": False,
		},
	]
	task_pages = [
		{
			"items": [
				{
					"id": "tsk_" + "c" * 22,
					"title": "Blank task",
					"overview": "  ",
				}
			],
			"limit": 1,
			"offset": 0,
			"has_more": True,
		},
		{
			"items": [],
			"limit": 1,
			"offset": 1,
			"has_more": False,
		},
	]

	def release_list(limit: int, offset: int, path=None):
		release_offsets.append(offset)
		return release_pages[offset]

	def task_list(status=None, limit=50, offset=0, path=None):
		task_offsets.append(offset)
		return task_pages[offset]

	monkeypatch.setattr(store, "release_list", release_list)
	monkeypatch.setattr(store, "task_list", task_list)

	result = store.doctor()

	assert result == {
		"findings": [
			{
				"field": "release.overview",
				"id": "rel_" + "b" * 22,
				"noun": "release",
				"title": "Blank release",
			},
			{
				"field": "task.overview",
				"id": "tsk_" + "c" * 22,
				"noun": "task",
				"title": "Blank task",
			},
		],
		"ok": False,
	}
	assert release_offsets == [0, 1]
	assert task_offsets == [0, 1]


def test_doctor_reports_clean_when_required_fields_are_populated(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	assert store.doctor() == {"findings": [], "ok": True}


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
