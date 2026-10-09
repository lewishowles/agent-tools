from pathlib import Path

import pytest

from agents_progress.database import Database
from agents_progress.errors import (
	InvalidObjectIdError,
	InvalidStatusError,
	NotFoundError,
	WrongObjectIdTypeError,
)
from agents_progress.ids import RELEASE_PREFIX, TASK_PREFIX
from agents_progress.projects import Project
from agents_progress.reads import ReadStore, resolve_identifier
from agents_progress.writes import WriteStore

PROJECT_ID = "prj_" + "p" * 22
RELEASE_A = "rel_" + "a" * 22
TASK_A = "tsk_" + "a" * 22
TASK_B = "tsk_" + "b" * 22
CHUNK_A = "chk_" + "a" * 22
DISCOVERY_A = "nte_" + "a" * 22
DISCOVERY_B = "nte_" + "b" * 22
DECISION_A = "nte_" + "c" * 22
INBOX_A = "inb_" + "a" * 22


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
				"id, project_id, slug, release_id, title, overview, verification, "
				"split_rationale, status, "
				"status_reason, position, created_at, started_at, completed_at, updated_at"
				") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
				(
					task_id,
					PROJECT_ID,
					slug,
					RELEASE_A,
					title,
					f"Overview for {slug}.",
					"Verification.",
					f"Split rationale for {slug}.",
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
				"INSERT INTO task_contract_steps (task_id, position, text) VALUES (?, ?, ?)",
				(task_id, 1, f"Contract for {slug}."),
			)
		connection.execute(
			"INSERT INTO chunks (id, task_id, position, title, description, status, started_at, completed_at, review_question) "
			"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
			(
				CHUNK_A,
				TASK_A,
				1,
				"Read surface",
				"Implement read queries.",
				"active",
				"2026-01-01T00:00:00+00:00",
				None,
				"Do the read queries return the stored records?",
			),
		)
		connection.execute(
			"UPDATE projects SET default_task_id = ? WHERE id = ?", (TASK_A, PROJECT_ID)
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
	assert [chunk["id"] for chunk in result["task"]["chunks"]] == [CHUNK_A]
	assert result["chunk"]["id"] == CHUNK_A
	assert result["hint_command"] == f"progress chunk complete {CHUNK_A}"


def test_next_named_ready_task_suggests_secondary_start_when_default_is_active(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	result = store.next(task_id=TASK_B)

	assert result["task"]["id"] == TASK_B
	assert result["hint_command"] == f"progress task start {TASK_B} --secondary"


def test_next_keeps_the_default_when_a_secondary_task_starts(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	third = writer.task_add(
		"third",
		"Third task",
		overview="Third task overview",
		contract=["Third task contract"],
	)
	writer.chunk_add(
		third["id"],
		"Third task chunk",
		description="Work on the third task.",
		review_question="Is the third task ready?",
	)
	fourth = writer.task_add(
		"fourth",
		"Fourth task",
		overview="Fourth task overview",
		contract=["Fourth task contract"],
	)
	fourth_chunk = writer.chunk_add(
		fourth["id"],
		"Fourth task chunk",
		description="Work on the fourth task.",
		review_question="Is the fourth task ready?",
	)
	writer.task_start(third["id"], secondary=True)
	writer.task_start(fourth["id"], secondary=True)

	result = store.next()

	assert result["task"]["id"] == TASK_A
	assert result["chunk"]["id"] == CHUNK_A
	assert store.next(task_id=fourth["id"])["chunk"]["id"] == fourth_chunk["id"]
	assert (
		ReadStore(store.database, _ProjectStore(store.database)).next()["task"]["id"]
		== TASK_A
	)


def test_next_named_task_rejects_done_unknown_and_other_project_tasks(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	other_project_id = "prj_" + "q" * 22
	other_task_id = "tsk_" + "q" * 22
	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_B,))
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, 'other', 'Other', ?)",
			(other_project_id, "2026-01-01T00:00:00+00:00"),
		)
		connection.execute(
			"INSERT INTO tasks (id, project_id, slug, title, overview, verification, split_rationale, status, position, created_at, updated_at) "
			"VALUES (?, ?, 'other', 'Other', 'Overview', 'Verification', 'Reason', 'ready', 1, ?, ?)",
			(
				other_task_id,
				other_project_id,
				"2026-01-01T00:00:00+00:00",
				"2026-01-01T00:00:00+00:00",
			),
		)

	with pytest.raises(InvalidStatusError):
		store.next(task_id=TASK_B)
	for task_id in ("tsk_" + "z" * 22, other_task_id):
		with pytest.raises(NotFoundError):
			store.next(task_id=task_id)


def test_summary_uses_the_next_selection_and_lists_every_project(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	second_project_id = "prj_" + "q" * 22
	completed_chunk_id = "chk_" + "b" * 22
	live_checkout = tmp_path / "agents"
	live_checkout.mkdir()
	stale_checkout = tmp_path / "missing"
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(second_project_id, "empty", "Empty project", "2026-01-01T00:00:00+00:00"),
		)
		connection.execute(
			"INSERT INTO checkouts (path, project_id, last_seen_at) VALUES (?, ?, ?)",
			(str(live_checkout), PROJECT_ID, "2026-01-02T12:00:00+00:00"),
		)
		connection.execute(
			"INSERT INTO checkouts (path, project_id, last_seen_at) VALUES (?, ?, ?)",
			(str(stale_checkout), PROJECT_ID, "2026-01-03T12:00:00+00:00"),
		)
		connection.execute(
			"UPDATE tasks SET status_reason = ? WHERE id = ?",
			("Finish the read query", TASK_A),
		)
		connection.execute(
			"INSERT INTO chunks (id, task_id, position, title, description, status, "
			"started_at, completed_at, review_question) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
			(
				completed_chunk_id,
				TASK_A,
				2,
				"Earlier chunk",
				"Completed before the active chunk.",
				"done",
				"2026-01-01T00:00:00+00:00",
				"2026-01-01T01:00:00+00:00",
				"Was the earlier work reviewed?",
			),
		)
		connection.execute("UPDATE chunks SET position = 3 WHERE id = ?", (CHUNK_A,))

	result = store.summary()
	next_result = store.next(include_position_totals=True)

	assert [item["project"]["name"] for item in result] == [
		"Agent configuration",
		"Empty project",
	]
	assert result[0]["task"]["id"] == store.next()["task"]["id"]
	assert result[0]["chunk"]["id"] == CHUNK_A
	assert result[0]["chunk_rank"] == next_result["chunk_rank"] == 2
	assert result[0]["chunk_total"] == next_result["chunk_total"] == 2
	assert "task_total" not in result[0]
	assert result[0]["hint_command"] == f"progress chunk complete {CHUNK_A}"
	assert result[0]["checkouts"] == [
		{
			"path": str(live_checkout),
			"last_seen_at": "2026-01-02T12:00:00+00:00",
			"stale": False,
		},
		{
			"path": str(stale_checkout),
			"last_seen_at": "2026-01-03T12:00:00+00:00",
			"stale": True,
		},
	]
	assert result[0]["commit_plan"] == {"done": 1, "total": 2}
	assert result[0]["other_task_counts"] == {"ready": 1}
	assert result[0]["release"]["id"] == RELEASE_A
	assert result[0]["next_action"] == "Finish the read query"
	assert result[1]["checkouts"] == []
	assert result[1]["task"] is None
	assert "chunk_rank" not in result[1]
	assert "chunk_total" not in result[1]
	assert result[1]["commit_plan"] is None
	assert result[1]["hint_command"] == "progress task list"


def test_next_uses_live_totals_and_ranks_after_sibling_removals(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	task = writer.task_add(
		"third",
		"Third task",
		overview="Third task overview",
		contract=["Third task contract"],
		release_id=RELEASE_A,
		position=3,
	)
	first_chunk = writer.chunk_add(
		task["id"],
		"First chunk",
		"First chunk description",
		review_question="Does it work?",
		position=1,
	)
	second_chunk = writer.chunk_add(
		task["id"],
		"Second chunk",
		"Second chunk description",
		review_question="Does it work?",
		position=2,
	)
	third_chunk = writer.chunk_add(
		task["id"],
		"Third chunk",
		"Third chunk description",
		review_question="Does it work?",
		position=3,
	)

	writer.discovery_remove(DISCOVERY_B)
	writer.task_remove(TASK_B)
	writer.chunk_remove(first_chunk["id"])
	writer.task_start(task["id"], secondary=True)
	writer.chunk_start(third_chunk["id"])

	assert store.task_count_for_release(RELEASE_A) == 2

	result = store.next(task_id=task["id"], include_position_totals=True)

	assert result["task"]["id"] == task["id"]
	assert [chunk["id"] for chunk in result["task"]["chunks"]] == [
		second_chunk["id"],
		third_chunk["id"],
	]
	assert result["task"]["position"] == 3
	assert result["task_rank"] == 2
	assert result["task_total"] == 2
	assert result["chunk"]["id"] == third_chunk["id"]
	assert result["chunk"]["position"] == 3
	assert result["chunk_rank"] == 2
	assert result["chunk_total"] == 2


def test_next_returns_the_earliest_ready_task_when_nothing_is_in_progress(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	first_ready = writer.task_add(
		"first-ready",
		"First ready",
		overview="First ready overview",
		contract=["First ready contract"],
		release_id=RELEASE_A,
		position=0,
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
		"planned",
		"Planned",
		overview="Planned release overview",
		status="planned",
		position=0,
	)
	writer.task_add(
		"planned-task",
		"Planned task",
		overview="Planned task overview",
		contract=["Planned task contract"],
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
		"blocked",
		"Blocked",
		overview="Blocked task overview",
		contract=["Blocked task contract"],
		release_id=RELEASE_A,
		depends_on=[TASK_A],
		position=3,
	)
	writer.task_block(blocked["id"], "Manual blocker")

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


def test_next_reports_the_earliest_waiting_task_and_its_unfinished_dependency(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	waiting = writer.task_add(
		"waiting",
		"Waiting",
		overview="Waiting task overview",
		contract=["Waiting task contract"],
		release_id=RELEASE_A,
		depends_on=[TASK_A, TASK_B],
		position=0,
	)

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_A,))

	before = store.task_get(waiting["id"])

	result = store.next()
	after = store.task_get(waiting["id"])

	assert result["task"]["id"] == waiting["id"]
	assert result["task"]["status"] == "waiting"
	assert result["dependency_ids"] == [TASK_A, TASK_B]
	assert result["chunk"] is None
	assert result["hint_command"] == f"progress task get {TASK_B}"
	assert after["status"] == before["status"]
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


def test_task_list_uses_position_then_object_id_and_pagination(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	result = store.task_list(limit=1)
	with_titles = store.task_list(include_release_titles=True)

	assert [item["id"] for item in result["items"]] == [TASK_A]
	assert "release_title" not in result["items"][0]
	assert with_titles["items"][0]["release_title"] == "Progress store"
	assert result["has_more"] is True


def test_task_list_numbers_unfinished_queue_independent_of_filters_and_pages(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	third = writer.task_add(
		"third",
		"Third task",
		overview="Third task overview.",
		contract=["Third task contract."],
		release_id=RELEASE_A,
		position=3,
	)
	fourth = writer.task_add(
		"fourth",
		"Fourth task",
		overview="Fourth task overview.",
		contract=["Fourth task contract."],
		release_id=RELEASE_A,
		position=4,
	)
	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_B,))

	all_items = store.task_list(include_queue_numbers=True)["items"]
	filtered_items = store.task_list(
		status="ready", offset=1, include_queue_numbers=True
	)["items"]
	done_items = store.task_list(status="done", include_queue_numbers=True)["items"]
	json_items = store.task_list()["items"]

	assert [(item["id"], item.get("queue_number")) for item in all_items] == [
		(TASK_A, 1),
		(TASK_B, None),
		(third["id"], 2),
		(fourth["id"], 3),
	]
	assert [(item["id"], item["queue_number"]) for item in filtered_items] == [
		(fourth["id"], 3)
	]
	assert "queue_number" not in done_items[0]
	assert all("queue_number" not in item for item in json_items)


def test_task_list_collapses_done_tasks_only_for_human_unfiltered_pages(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (TASK_B,))

	page = store.task_list(limit=1, collapse_done_tasks=True)
	filtered = store.task_list(status="done", collapse_done_tasks=True)
	all_tasks = store.task_list(limit=1, collapse_done_tasks=True, show_all=True)
	json_page = store.task_list(limit=1)

	assert [item["id"] for item in page["items"]] == [TASK_A]
	assert page["has_more"] is False
	assert page["done_counts"] == {RELEASE_A: 1}
	assert page["status_counts"] == {"in-progress": 1, "done": 1}
	assert page["next_task"] == "First task"
	assert [item["id"] for item in filtered["items"]] == [TASK_B]
	assert filtered["done_counts"] == {}
	assert [item["id"] for item in all_tasks["items"]] == [TASK_A, TASK_B]
	assert all_tasks["limit"] is None
	assert all_tasks["has_more"] is False
	assert "done_counts" not in json_page
	assert json_page["has_more"] is True


def test_task_list_keeps_unassigned_release_id_in_human_counts(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE tasks SET release_id = NULL, status = 'done' WHERE id = ?",
			(TASK_B,),
		)

	result = store.task_list(collapse_done_tasks=True)

	assert result["done_counts"] == {None: 1}
	assert result["release_order"][-1] == {"id": None, "title": None}


def test_chunk_list_collapses_done_chunks_before_paging(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	chunk_ids = ["chk_" + letter * 22 for letter in "bcd"]
	with store.database.transaction() as connection:
		connection.execute("UPDATE chunks SET status = 'done' WHERE id = ?", (CHUNK_A,))
		for position, chunk_id, status, title in (
			(2, chunk_ids[0], "active", "Active work"),
			(3, chunk_ids[1], "skipped", "Skipped work"),
			(4, chunk_ids[2], "pending", "Pending work"),
		):
			connection.execute(
				"INSERT INTO chunks (id, task_id, position, title, description, status) "
				"VALUES (?, ?, ?, ?, ?, ?)",
				(chunk_id, TASK_A, position, title, title, status),
			)

	first_page = store.chunk_list(TASK_A, limit=2, collapse_done_chunks=True)
	second_page = store.chunk_list(TASK_A, limit=2, offset=2, collapse_done_chunks=True)
	all_chunks = store.chunk_list(
		TASK_A, limit=1, collapse_done_chunks=True, show_all=True
	)
	json_page = store.chunk_list(TASK_A, limit=2)

	assert [item["id"] for item in first_page["items"]] == chunk_ids[:2]
	assert first_page["has_more"] is True
	assert first_page["done_count"] == 1
	assert first_page["status_counts"] == {
		"done": 1,
		"active": 1,
		"skipped": 1,
		"pending": 1,
	}
	assert first_page["next_chunk"] == "Active work"
	assert [item["id"] for item in second_page["items"]] == chunk_ids[2:]
	assert second_page["has_more"] is False
	assert [item["id"] for item in all_chunks["items"]] == [CHUNK_A, *chunk_ids]
	assert all_chunks["done_count"] == 0
	assert all_chunks["limit"] is None
	assert all_chunks["offset"] == 0
	assert all_chunks["has_more"] is False
	assert [item["id"] for item in json_page["items"]] == [CHUNK_A, chunk_ids[0]]
	assert "status_counts" not in json_page
	assert "done_count" not in json_page
	assert "next_chunk" not in json_page


def test_chunk_list_uses_first_pending_when_no_chunk_is_active(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE chunks SET status = 'pending' WHERE id = ?", (CHUNK_A,)
		)

	result = store.chunk_list(TASK_A, collapse_done_chunks=True)

	assert result["next_chunk"] == "Read surface"


def test_task_list_filters_waiting_tasks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	waiting = writer.task_add(
		"waiting",
		"Waiting",
		overview="Waiting task overview",
		contract=["Waiting task contract"],
		release_id=RELEASE_A,
		depends_on=[TASK_A],
	)

	result = store.task_list(status="waiting")

	assert [task["id"] for task in result["items"]] == [waiting["id"]]
	assert result["has_more"] is False


def test_task_list_leaves_unassigned_tasks_without_release_titles(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET release_id = NULL WHERE id = ?", (TASK_B,))

	result = store.task_list(include_release_titles=True)
	assigned = next(item for item in result["items"] if item["id"] == TASK_A)
	unassigned = next(item for item in result["items"] if item["id"] == TASK_B)

	assert assigned["release_title"] == "Progress store"
	assert unassigned["release_id"] is None
	assert "release_title" not in unassigned


def test_search_reports_a_matching_task_file_path(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO task_files (task_id, position, text) VALUES (?, ?, ?)",
			(TASK_A, 1, "packages/button.py"),
		)

	result = store.search("button")

	assert result["items"] == [
		{
			"type": "task",
			"id": TASK_A,
			"title": "First task",
			"status": "in-progress",
			"matched": ["files"],
			"snippets": {"files": ["packages/button.py"]},
		}
	]


def test_search_reports_a_matching_task_contract_step(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO task_contract_steps (task_id, position, text) VALUES (?, ?, ?)",
			(TASK_A, 2, "The button contract is ready."),
		)

	item = store.search("button", fields=["contract"])["items"][0]

	assert item["matched"] == ["contract"]
	assert item["snippets"]["contract"] == "The button contract is ready."


def test_search_reports_a_matching_chunk_description(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE chunks SET description = ? WHERE id = ?",
			("Implement the button read query.", CHUNK_A),
		)

	result = store.search("button", fields=["description"])

	assert result["items"] == [
		{
			"type": "chunk",
			"id": CHUNK_A,
			"title": "Read surface",
			"status": "active",
			"task_id": TASK_A,
			"task_title": "First task",
			"matched": ["description"],
			"snippets": {"description": "Implement the button read query."},
		}
	]


def test_search_in_fields_narrows_matches(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO task_files (task_id, position, text) VALUES (?, ?, ?)",
			(TASK_A, 1, "packages/button.py"),
		)
		connection.execute(
			"UPDATE chunks SET description = ? WHERE id = ?",
			("Implement the button read query.", CHUNK_A),
		)

	assert store.search("button", fields=["title"])["items"] == []
	assert [
		item["type"] for item in store.search("button", fields=["files"])["items"]
	] == ["task"]


def test_search_status_filters_tasks_and_chunk_parents(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE tasks SET overview = ?, status = ? WHERE id = ?",
			("Done button task.", "done", TASK_B),
		)
		connection.execute(
			"UPDATE chunks SET description = ? WHERE id = ?",
			("In-progress button chunk.", CHUNK_A),
		)

	result = store.search("button", status="done")

	assert [item["id"] for item in result["items"]] == [TASK_B]


def test_search_snippet_has_bounded_context_on_both_sides(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	overview = "prefix words " * 8 + "button" + " suffix words" * 8

	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE tasks SET overview = ? WHERE id = ?",
			(overview, TASK_A),
		)

	snippet = store.search("button", fields=["overview"])["items"][0]["snippets"][
		"overview"
	]

	assert isinstance(snippet, str)
	assert snippet.startswith("...")
	assert snippet.endswith("...")
	assert "button" in snippet


def test_search_snippet_keeps_character_context_without_word_boundaries(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	overview = "x" * 60 + "button" + "y" * 60

	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE tasks SET overview = ? WHERE id = ?",
			(overview, TASK_A),
		)

	snippet = store.search("button", fields=["overview"])["items"][0]["snippets"][
		"overview"
	]

	assert snippet == "..." + "x" * 40 + "button" + "y" * 40 + "..."


@pytest.mark.parametrize("term", ["a_b", "a%b"])
def test_search_escapes_like_wildcards(tmp_path: Path, term: str) -> None:
	store = _seed_store(tmp_path)
	other_term = term.replace("_", "x").replace("%", "x")

	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE tasks SET overview = ? WHERE id = ?",
			(f"Literal {term}.", TASK_A),
		)
		connection.execute(
			"UPDATE tasks SET overview = ? WHERE id = ?",
			(f"Literal {other_term}.", TASK_B),
		)

	result = store.search(term, fields=["overview"])

	assert [item["id"] for item in result["items"]] == [TASK_A]


def test_search_returns_an_empty_page_when_nothing_matches(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	assert store.search("missing term") == {
		"items": [],
		"limit": 50,
		"offset": 0,
		"has_more": False,
	}


def test_task_reads_return_contract_files_and_split_rationale_in_order(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = WriteStore(store.database, _ProjectStore(store.database)).task_add(
		"ordered",
		"Ordered task",
		overview="Ordered task overview",
		contract=["First step", "Second step"],
		files=["src/first.py", "src/second.py"],
		split_rationale="Keep each step reviewable",
	)

	assert store.task_get(task["id"])["contract"] == ["First step", "Second step"]
	assert store.task_get(task["id"])["files"] == ["src/first.py", "src/second.py"]
	assert store.task_get(task["id"])["split_rationale"] == "Keep each step reviewable"
	listed = next(
		item for item in store.task_list()["items"] if item["id"] == task["id"]
	)

	assert listed["contract"] == ["First step", "Second step"]
	assert listed["files"] == ["src/first.py", "src/second.py"]
	assert listed["split_rationale"] == "Keep each step reviewable"


def test_doctor_reports_blank_required_fields_across_all_pages(
	tmp_path: Path, monkeypatch
) -> None:
	store = _seed_store(tmp_path)
	release_offsets: list[int] = []
	task_offsets: list[int] = []
	chunk_offsets: list[int] = []
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
					"contract": [" \t"],
					"split_rationale": "",
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
	chunk_pages = [
		{
			"items": [
				{
					"id": "chk_" + "d" * 22,
					"title": "Blank chunk",
					"description": "",
					"status": "pending",
				},
				{
					"id": "chk_" + "e" * 22,
					"title": "Finished chunk",
					"description": "Finished before review questions existed.",
					"status": "done",
				},
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

	def release_list(limit: int, offset: int, path=None, *, show_all: bool = False):
		assert show_all is True
		release_offsets.append(offset)
		return release_pages[offset]

	def task_list(status=None, limit=50, offset=0, path=None):
		task_offsets.append(offset)
		return task_pages[offset]

	def chunk_list(limit, offset, path=None):
		chunk_offsets.append(offset)
		return chunk_pages[offset]

	monkeypatch.setattr(store, "release_list", release_list)
	monkeypatch.setattr(store, "task_list", task_list)
	monkeypatch.setattr(store, "_chunk_list_for_project", chunk_list)

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
			{
				"field": "task.contract",
				"id": "tsk_" + "c" * 22,
				"noun": "task",
				"title": "Blank task",
			},
			{
				"field": "task.split_rationale",
				"id": "tsk_" + "c" * 22,
				"noun": "task",
				"title": "Blank task",
			},
			{
				"field": "chunk.description",
				"id": "chk_" + "d" * 22,
				"noun": "chunk",
				"title": "Blank chunk",
			},
			{
				"field": "chunk.review_question",
				"id": "chk_" + "d" * 22,
				"noun": "chunk",
				"title": "Blank chunk",
			},
		],
		"ok": False,
	}
	assert release_offsets == [0, 1]
	assert task_offsets == [0, 1]
	assert chunk_offsets == [0, 1]


def test_doctor_reports_clean_when_required_fields_are_populated(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	assert store.doctor() == {"findings": [], "ok": True}


def test_doctor_reports_a_blank_overview_for_a_done_release(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE releases SET overview = ?, status = ? WHERE id = ?",
			("", "done", RELEASE_A),
		)

	assert store.doctor() == {
		"findings": [
			{
				"field": "release.overview",
				"id": RELEASE_A,
				"noun": "release",
				"title": "Progress store",
			}
		],
		"ok": False,
	}


def test_task_get_rejects_a_wrong_object_type_before_lookup(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(WrongObjectIdTypeError):
		store.task_get(CHUNK_A)


def test_project_get_returns_only_the_current_project(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	other_project_id = "prj_" + "q" * 22

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(other_project_id, "other", "Other project", "2026-01-02T00:00:00+00:00"),
		)

	project = store.project_get(PROJECT_ID)

	assert project == {
		"id": PROJECT_ID,
		"slug": "agents",
		"name": "Agent configuration",
	}

	with pytest.raises(NotFoundError):
		store.project_get(other_project_id)


@pytest.mark.parametrize(
	("note_id", "note_type", "body"),
	[
		(DISCOVERY_A, "discovery", "First discovery."),
		(DECISION_A, "decision", "First decision."),
	],
)
def test_note_get_returns_the_note_and_its_kind(
	tmp_path: Path, note_id: str, note_type: str, body: str
) -> None:
	store = _seed_store(tmp_path)

	note = store.note_get(note_id)

	assert note["id"] == note_id
	assert note["project_id"] == PROJECT_ID
	assert note["task_id"] == TASK_A
	assert note["type"] == note_type
	assert note["body"] == body


def test_inbox_get_returns_the_stored_note(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO inbox_notes (id, project_id, text, created_at) VALUES (?, ?, ?, ?)",
			(INBOX_A, PROJECT_ID, "Follow up", "2026-01-02T00:00:00+00:00"),
		)

	note = store.inbox_get(INBOX_A)

	assert note == {
		"id": INBOX_A,
		"project_id": PROJECT_ID,
		"text": "Follow up",
		"created_at": "2026-01-02T00:00:00+00:00",
	}


def test_note_and_inbox_get_reject_other_project_records(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	other_project_id = "prj_" + "q" * 22
	other_release_id = "rel_" + "b" * 22
	other_note_id = "nte_" + "d" * 22
	other_inbox_id = "inb_" + "b" * 22

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(other_project_id, "other", "Other project", "2026-01-02T00:00:00+00:00"),
		)
		connection.execute(
			"INSERT INTO releases (id, project_id, slug, title, overview, status, position) "
			"VALUES (?, ?, ?, ?, ?, ?, ?)",
			(
				other_release_id,
				other_project_id,
				"other",
				"Other release",
				"Overview",
				"planned",
				1,
			),
		)
		connection.execute(
			"INSERT INTO notes (id, project_id, release_id, type, body, created_at) "
			"VALUES (?, ?, ?, ?, ?, ?)",
			(
				other_note_id,
				other_project_id,
				other_release_id,
				"discovery",
				"Other note",
				"2026-01-02T00:00:00+00:00",
			),
		)
		connection.execute(
			"INSERT INTO inbox_notes (id, project_id, text, created_at) VALUES (?, ?, ?, ?)",
			(
				other_inbox_id,
				other_project_id,
				"Other inbox note",
				"2026-01-02T00:00:00+00:00",
			),
		)

	with pytest.raises(NotFoundError):
		store.note_get(other_note_id)

	with pytest.raises(NotFoundError):
		store.inbox_get(other_inbox_id)


@pytest.mark.parametrize(
	("method_name", "missing_id", "malformed_id"),
	[
		("project_get", "prj_" + "z" * 22, "prj_short"),
		("note_get", "nte_" + "z" * 22, "nte_short"),
		("inbox_get", "inb_" + "z" * 22, "inb_short"),
	],
)
def test_project_note_and_inbox_get_reject_wrong_type_missing_and_malformed_ids(
	tmp_path: Path, method_name: str, missing_id: str, malformed_id: str
) -> None:
	store = _seed_store(tmp_path)
	get_record = getattr(store, method_name)

	with pytest.raises(WrongObjectIdTypeError):
		get_record(TASK_A)

	with pytest.raises(NotFoundError, match="was not found"):
		get_record(missing_id)

	with pytest.raises(InvalidObjectIdError):
		get_record(malformed_id)


@pytest.mark.parametrize(
	("value", "expected_prefix", "expected_id"),
	[
		(TASK_A, TASK_PREFIX, TASK_A),
		("first", TASK_PREFIX, TASK_A),
		("missing-task", TASK_PREFIX, "missing-task"),
		(RELEASE_A, RELEASE_PREFIX, RELEASE_A),
		("progress-store", RELEASE_PREFIX, RELEASE_A),
		("missing-release", RELEASE_PREFIX, "missing-release"),
	],
)
def test_resolve_identifier_tries_id_then_project_slug(
	tmp_path: Path,
	value: str,
	expected_prefix: str,
	expected_id: str,
) -> None:
	store = _seed_store(tmp_path)

	with store.database.connection() as connection:
		resolved_id = resolve_identifier(connection, value, expected_prefix, PROJECT_ID)

	assert resolved_id == expected_id


def test_resolve_identifier_rejects_a_wrong_object_type(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with (
		store.database.connection() as connection,
		pytest.raises(WrongObjectIdTypeError),
	):
		resolve_identifier(connection, CHUNK_A, TASK_PREFIX, PROJECT_ID)


@pytest.mark.parametrize(
	("reference", "expected_id"),
	[(RELEASE_A, RELEASE_A), ("progress-store", RELEASE_A)],
)
def test_release_get_accepts_an_id_or_slug(
	tmp_path: Path, reference: str, expected_id: str
) -> None:
	store = _seed_store(tmp_path)

	release = store.release_get(reference)

	assert release["id"] == expected_id


@pytest.mark.parametrize(
	("reference", "expected_id"),
	[(TASK_A, TASK_A), ("first", TASK_A)],
)
def test_task_get_accepts_an_id_or_slug(
	tmp_path: Path, reference: str, expected_id: str
) -> None:
	store = _seed_store(tmp_path)

	task = store.task_get(reference)

	assert task["id"] == expected_id
	assert isinstance(task["chunks"], list)
	assert [chunk["id"] for chunk in task["chunks"]] == (
		[CHUNK_A] if expected_id == TASK_A else []
	)


def test_task_get_includes_its_release_notes_and_handoff(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO notes (id, project_id, release_id, type, body, created_at) "
			"VALUES (?, ?, ?, ?, ?, ?)",
			(
				"nte_" + "r" * 22,
				PROJECT_ID,
				RELEASE_A,
				"decision",
				"Release decision.",
				"2026-01-01T00:00:00+00:00",
			),
		)
		connection.execute(
			"UPDATE notes SET supersedes_id = ? WHERE id = ?",
			(DISCOVERY_A, DECISION_A),
		)
		connection.execute(
			"INSERT INTO task_context (task_id, current_goal, next_step, updated_at) "
			"VALUES (?, ?, ?, ?)",
			(TASK_A, "Finish reads", "Run tests", "2026-01-01T00:00:04+00:00"),
		)

	task = store.task_get(TASK_A)

	assert task["project"] == {
		"id": PROJECT_ID,
		"slug": "agents",
		"name": "Agent configuration",
	}
	assert task["release"]["id"] == RELEASE_A
	assert [note["body"] for note in task["release"]["notes"]] == ["Release decision."]
	assert [note["id"] for note in task["notes"]] == [DISCOVERY_A, DECISION_A]
	assert task["notes"][1]["supersedes_id"] == DISCOVERY_A
	assert task["handoff"] == store.context_get(task_id=TASK_A)


def test_task_get_without_a_release_or_handoff_uses_empty_context(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	with store.database.transaction() as connection:
		connection.execute("UPDATE tasks SET release_id = NULL WHERE id = ?", (TASK_B,))
		connection.execute("DELETE FROM notes WHERE task_id = ?", (TASK_B,))
		connection.execute(
			"INSERT INTO context (project_id, current_goal, updated_at) VALUES (?, ?, ?)",
			(PROJECT_ID, "Project handoff", "2026-01-01T00:00:04+00:00"),
		)

	task = store.task_get(TASK_B)

	assert task["release"] is None
	assert task["notes"] == []
	assert task["handoff"] == {
		"status": "not-set",
		"project_id": PROJECT_ID,
		"task_id": TASK_B,
	}


@pytest.mark.parametrize("reference", ["missing-release", "rel_" + "r" * 22])
def test_release_get_rejects_an_unknown_identifier(
	tmp_path: Path, reference: str
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(NotFoundError):
		store.release_get(reference)


@pytest.mark.parametrize("reference", ["missing-task", "tsk_" + "t" * 22])
def test_task_get_rejects_an_unknown_identifier(tmp_path: Path, reference: str) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(NotFoundError):
		store.task_get(reference)


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


@pytest.mark.parametrize("task_reference", [TASK_A, "first"])
def test_chunk_list_and_position_get_use_the_same_task_positions(
	tmp_path: Path, task_reference: str
) -> None:
	store = _seed_store(tmp_path)
	second_chunk_id = "chk_" + "b" * 22
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO chunks (id, task_id, position, title, description, status) "
			"VALUES (?, ?, ?, ?, ?, ?)",
			(
				second_chunk_id,
				TASK_A,
				2,
				"Second chunk",
				"Continue reading.",
				"pending",
			),
		)

	listed = store.chunk_list(task_reference)["items"]
	first = store.chunk_get_by_position(task_reference, 1)
	second = store.chunk_get_by_position(task_reference, 2)

	assert [(chunk["id"], chunk["position"]) for chunk in listed] == [
		(CHUNK_A, 1),
		(second_chunk_id, 2),
	]
	assert first["id"] == CHUNK_A
	assert second["id"] == second_chunk_id


@pytest.mark.parametrize("task_reference", [TASK_A, "first"])
def test_chunk_get_by_position_names_the_missing_task_position(
	tmp_path: Path, task_reference: str
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(NotFoundError) as error:
		store.chunk_get_by_position(task_reference, 2)

	assert TASK_A in error.value.message
	assert "position 2" in error.value.message
	assert error.value.details == {"task_id": TASK_A, "position": 2}


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


def test_inbox_list_orders_notes_and_scopes_them_to_the_project(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	other_project_id = "prj_" + "q" * 22
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(other_project_id, "other", "Other project", "2026-01-01T00:00:00+00:00"),
		)
		for note_id, project_id, text, created_at in (
			(
				"inb_" + "b" * 22,
				PROJECT_ID,
				"Second by ID",
				"2026-01-01T00:00:01+00:00",
			),
			("inb_" + "c" * 22, PROJECT_ID, "Newest", "2026-01-01T00:00:02+00:00"),
			("inb_" + "a" * 22, PROJECT_ID, "First by ID", "2026-01-01T00:00:01+00:00"),
			(
				"inb_" + "d" * 22,
				other_project_id,
				"Other project",
				"2026-01-01T00:00:00+00:00",
			),
		):
			connection.execute(
				"INSERT INTO inbox_notes (id, project_id, text, created_at) VALUES (?, ?, ?, ?)",
				(note_id, project_id, text, created_at),
			)

	first_page = store.inbox_list(limit=2)
	second_page = store.inbox_list(limit=2, offset=2)

	assert [item["text"] for item in first_page["items"]] == [
		"First by ID",
		"Second by ID",
	]
	assert [item["text"] for item in second_page["items"]] == ["Newest"]
	assert all(item["project_id"] == PROJECT_ID for item in first_page["items"])
	assert first_page["has_more"] is True
	assert second_page["has_more"] is False


def test_note_lists_filter_type_and_optional_task_or_release_in_creation_order(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	writer = WriteStore(store.database, _ProjectStore(store.database))
	release_discovery = writer.discovery_add(
		None, "Release discovery.", release_id=RELEASE_A
	)
	release_decision = writer.decision_add(
		None, "Release decision.", release_id=RELEASE_A
	)

	discoveries = store.discovery_list()
	task_discoveries = store.discovery_list(TASK_A)
	release_discoveries = store.discovery_list(release_id=RELEASE_A)
	decisions = store.decision_list(TASK_A)
	release_decisions = store.decision_list(release_id=RELEASE_A)
	empty = store.decision_list(TASK_B)

	assert [item["id"] for item in discoveries["items"]] == [
		DISCOVERY_A,
		DISCOVERY_B,
		release_discovery["id"],
	]
	assert [item["type"] for item in discoveries["items"]] == [
		"discovery",
		"discovery",
		"discovery",
	]
	assert [item["id"] for item in task_discoveries["items"]] == [DISCOVERY_A]
	assert [item["id"] for item in release_discoveries["items"]] == [
		release_discovery["id"]
	]
	assert [item["id"] for item in decisions["items"]] == [DECISION_A]
	assert [item["id"] for item in release_decisions["items"]] == [
		release_decision["id"]
	]
	assert empty["items"] == []
	assert empty["has_more"] is False


def test_context_get_returns_a_not_set_result_without_a_context_row(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	assert store.context_get() == {
		"status": "not-set",
		"project_id": PROJECT_ID,
		"task_id": TASK_A,
	}


def test_context_get_returns_the_default_task_context(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO task_context ("
			"task_id, current_goal, previous_step, next_step, standing_context, "
			"verify_with, stop_marker, updated_at"
			") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
			(
				TASK_A,
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
		"task_id": TASK_A,
		"current_goal": "Finish reads",
		"previous_step": "Inspect queries",
		"next_step": "Run tests",
		"standing_context": "Keep changes narrow",
		"verify_with": "test:unit",
		"stop_marker": "Stop after verification",
		"updated_at": "2026-01-01T00:00:04+00:00",
	}


def test_context_get_uses_project_context_without_an_in_progress_default(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO context (project_id, current_goal, updated_at) VALUES (?, ?, ?)",
			(PROJECT_ID, "Project planning", "2026-01-01T00:00:04+00:00"),
		)
		connection.execute(
			"UPDATE tasks SET status = 'blocked' WHERE id = ?", (TASK_A,)
		)

	assert store.context_get()["current_goal"] == "Project planning"
	assert store.context_get()["task_id"] is None


def test_context_get_selects_a_named_task_beside_the_default(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO task_context (task_id, current_goal, updated_at) "
			"VALUES (?, ?, ?), (?, ?, ?)",
			(
				TASK_A,
				"Default work",
				"2026-01-01T00:00:04+00:00",
				TASK_B,
				"Named work",
				"2026-01-01T00:00:05+00:00",
			),
		)

	assert store.context_get()["current_goal"] == "Default work"
	assert store.context_get(task_id=TASK_B)["current_goal"] == "Named work"
	assert store.context_get(task_id="second")["task_id"] == TASK_B


def test_context_get_reports_a_named_task_without_a_handoff(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	assert store.context_get(task_id=TASK_B) == {
		"status": "not-set",
		"project_id": PROJECT_ID,
		"task_id": TASK_B,
	}


def test_context_get_refuses_a_task_from_another_project(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	other_project_id = "prj_" + "o" * 22
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(other_project_id, "other", "Other", "2026-01-01T00:00:00+00:00"),
		)
		connection.execute(
			"UPDATE tasks SET project_id = ? WHERE id = ?",
			(other_project_id, TASK_B),
		)

	with pytest.raises(NotFoundError, match="task"):
		store.context_get(task_id=TASK_B)
