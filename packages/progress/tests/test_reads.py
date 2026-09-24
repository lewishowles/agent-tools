from pathlib import Path

import pytest

from agents_progress.database import Database
from agents_progress.errors import NotFoundError, WrongObjectIdTypeError
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
	assert [chunk["id"] for chunk in result["task"]["chunks"]] == [CHUNK_A]
	assert result["chunk"]["id"] == CHUNK_A
	assert result["hint_command"] == f"progress chunk complete {CHUNK_A}"


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
		task["id"], "First chunk", "First chunk description", position=1
	)
	second_chunk = writer.chunk_add(
		task["id"], "Second chunk", "Second chunk description", position=2
	)
	third_chunk = writer.chunk_add(
		task["id"], "Third chunk", "Third chunk description", position=3
	)

	writer.discovery_remove(DISCOVERY_B)
	writer.task_remove(TASK_B)
	writer.chunk_remove(first_chunk["id"])
	writer.task_start(task["id"])
	writer.chunk_start(third_chunk["id"])

	assert store.task_count_for_release(RELEASE_A) == 2
	assert store.chunk_count_for_task(task["id"]) == 2

	result = store.next(include_position_totals=True)

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
