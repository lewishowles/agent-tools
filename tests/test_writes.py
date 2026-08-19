from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
import sqlite3

import pytest

from agents_progress.database import Database
from agents_progress import database as database_module
from agents_progress.errors import (
	DatabaseBusyError,
	InvalidDependencyError,
	InvalidTransitionError,
	PendingChunksError,
	ProgressError,
	StillReferencedError,
	UnresolvedDependenciesError,
)
from agents_progress.projects import Project
from agents_progress.reads import ReadStore
from agents_progress.writes import WriteStore


PROJECT_ID = "prj_" + "p" * 22


class _ProjectStore:
	"""Return a seeded project without requiring a Git repository in unit tests."""

	def __init__(self, database: Database) -> None:
		self.database = database

	def current(self, path: str | Path | None = None) -> Project:
		return Project(
			PROJECT_ID, "agents", "Agent configuration", "2026-01-01T00:00:00+00:00"
		)


def _seed_store(tmp_path: Path) -> WriteStore:
	database = Database(tmp_path / "progress.db")
	with database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(PROJECT_ID, "agents", "Agent configuration", "2026-01-01T00:00:00+00:00"),
		)

	return WriteStore(database, _ProjectStore(database))


def _add_release_in_process(database_path: str, slug: str, results) -> None:
	try:
		database = Database(database_path)
		result = WriteStore(database, _ProjectStore(database)).release_add(
			slug, slug.title()
		)
	except Exception as error:
		results.put(("error", getattr(error, "code", type(error).__name__)))
	else:
		results.put(("ok", result["slug"]))


def test_explicit_zero_positions_are_preserved(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("zero-release", "Zero release", position=0)
	task = store.task_add(
		"zero-task", "Zero task", release_id=release["id"], position=0
	)
	chunk = store.chunk_add(task["id"], "Zero chunk", position=0)

	assert release["position"] == 0
	assert task["position"] == 0
	assert chunk["position"] == 0


def test_task_defaults_use_the_next_free_position_in_each_queue(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release")
	store.task_add("first", "First", release_id=release["id"], position=1)
	store.task_add("third", "Third", release_id=release["id"], position=3)
	store.task_add("unassigned-first", "Unassigned first", position=1)

	release_task = store.task_add("second", "Second", release_id=release["id"])
	unassigned_task = store.task_add("unassigned-second", "Unassigned second")

	assert release_task["position"] == 2
	assert unassigned_task["position"] == 2


def test_task_move_reorders_only_the_task_queue(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release")
	first = store.task_add("first", "First", release_id=release["id"])
	second = store.task_add("second", "Second", release_id=release["id"])
	third = store.task_add("third", "Third", release_id=release["id"])
	unassigned = store.task_add("unassigned", "Unassigned")

	moved = store.task_move(third["id"], before_task_id=first["id"])
	ordered = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert moved["id"] == third["id"]
	assert moved["position"] == 1
	assert [
		item["id"] for item in ordered["items"] if item["release_id"] == release["id"]
	] == [
		third["id"],
		first["id"],
		second["id"],
	]
	assert unassigned["position"] == 1

	store.task_move(first["id"], after_task_id=second["id"])
	ordered = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert [
		item["id"] for item in ordered["items"] if item["release_id"] == release["id"]
	] == [
		third["id"],
		second["id"],
		first["id"],
	]


def test_task_move_rejects_tasks_from_different_releases(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first_release = store.release_add("first-release", "First release")
	second_release = store.release_add("second-release", "Second release")
	first_task = store.task_add(
		"first-task", "First task", release_id=first_release["id"]
	)
	second_task = store.task_add(
		"second-task", "Second task", release_id=second_release["id"]
	)

	with pytest.raises(InvalidTransitionError, match="same release"):
		store.task_move(first_task["id"], before_task_id=second_task["id"])


def test_chunk_defaults_use_the_next_free_position(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("chunk-positions", "Chunk positions")
	first = store.chunk_add(task["id"], "First", position=1)
	third = store.chunk_add(task["id"], "Third", position=3)
	second = store.chunk_add(task["id"], "Second")

	assert [first["position"], second["position"], third["position"]] == [1, 2, 3]


def test_chunk_move_reorders_only_the_task_chunks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("chunk-move", "Chunk move")
	first = store.chunk_add(task["id"], "First")
	second = store.chunk_add(task["id"], "Second")
	third = store.chunk_add(task["id"], "Third")
	other_task = store.task_add("other-task", "Other task")
	other_chunk = store.chunk_add(other_task["id"], "Other chunk")

	moved = store.chunk_move(third["id"], before_chunk_id=first["id"])
	ordered = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert moved["id"] == third["id"]
	assert moved["position"] == 1
	assert [item["id"] for item in ordered["items"]] == [
		third["id"],
		first["id"],
		second["id"],
	]
	assert other_chunk["position"] == 1

	store.chunk_move(first["id"], after_chunk_id=second["id"])
	ordered = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert [item["id"] for item in ordered["items"]] == [
		third["id"],
		second["id"],
		first["id"],
	]


def test_chunk_move_rejects_chunks_from_different_tasks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first_task = store.task_add("first-task", "First task")
	second_task = store.task_add("second-task", "Second task")
	first_chunk = store.chunk_add(first_task["id"], "First chunk")
	second_chunk = store.chunk_add(second_task["id"], "Second chunk")

	with pytest.raises(InvalidTransitionError, match="same task"):
		store.chunk_move(first_chunk["id"], before_chunk_id=second_chunk["id"])


def test_creation_and_chunk_lifecycle_are_atomic(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Progress store", "Store progress.")
	task = store.task_add(
		"lifecycle", "Lifecycle", release_id=release["id"], purpose="Run lifecycle."
	)
	first_chunk = store.chunk_add(task["id"], "First", "First chunk.")
	second_chunk = store.chunk_add(task["id"], "Second", "Second chunk.")

	started = store.task_start(task["id"])
	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert started["status"] == "in-progress"
	assert [chunk["status"] for chunk in chunks["items"]] == ["active", "pending"]

	completed_first = store.chunk_complete(first_chunk["id"])
	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert completed_first["status"] == "done"
	assert chunks["items"][1]["status"] == "active"
	with pytest.raises(PendingChunksError):
		store.task_complete(task["id"])

	store.chunk_complete(second_chunk["id"])
	completed_task = store.task_complete(task["id"])

	assert completed_task["status"] == "done"
	assert completed_task["completed_at"] is not None


def test_chunk_start_demotes_active_chunk_and_activates_target(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("chunk-start", "Chunk start")
	active_chunk = store.chunk_add(task["id"], "Active chunk")
	pending_chunk = store.chunk_add(task["id"], "Pending chunk")
	store.task_start(task["id"])

	started = store.chunk_start(pending_chunk["id"])
	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)
	chunks_by_id = {chunk["id"]: chunk for chunk in chunks["items"]}

	assert started["id"] == pending_chunk["id"]
	assert started["status"] == "active"
	assert chunks_by_id[active_chunk["id"]]["status"] == "pending"
	assert chunks_by_id[active_chunk["id"]]["started_at"] is None
	assert chunks_by_id[pending_chunk["id"]]["started_at"] is not None


def test_chunk_start_requires_an_in_progress_task(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("ready-task", "Ready task")
	chunk = store.chunk_add(task["id"], "Pending chunk")

	with pytest.raises(InvalidTransitionError, match="progress task start"):
		store.chunk_start(chunk["id"])


@pytest.mark.parametrize("status", ["active", "done", "skipped"])
def test_chunk_start_rejects_non_pending_chunks(tmp_path: Path, status: str) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add(f"{status}-task", f"{status.title()} task")
	chunk = store.chunk_add(task["id"], "Chunk")
	store.task_start(task["id"])

	if status == "done":
		store.chunk_complete(chunk["id"])
	elif status == "skipped":
		with store.database.transaction() as connection:
			connection.execute(
				"UPDATE chunks SET status = 'skipped' WHERE id = ?", (chunk["id"],)
			)

	with pytest.raises(InvalidTransitionError, match="must be pending"):
		store.chunk_start(chunk["id"])


def test_dependencies_block_and_unblock_tasks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	dependency = store.task_add("dependency", "Dependency")
	dependent = store.task_add("dependent", "Dependent", depends_on=[dependency["id"]])

	assert dependent["status"] == "blocked"
	assert "unresolved dependencies" in dependent["status_reason"]
	with pytest.raises(UnresolvedDependenciesError):
		store.task_unblock(dependent["id"])

	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	ready_dependent = store.task_unblock(dependent["id"])

	assert ready_dependent["status"] == "ready"
	assert ready_dependent["status_reason"] is None


def test_starting_a_second_task_demotes_the_first_to_ready(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = store.task_add("first", "First")
	first_chunk = store.chunk_add(first["id"], "Chunk")
	second = store.task_add("second", "Second")
	store.task_start(first["id"])

	started_second = store.task_start(second["id"])
	demoted_first = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		first["id"]
	)
	first_chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		first["id"]
	)

	assert started_second["status"] == "in-progress"
	assert started_second["demoted_task"] == {
		"id": first["id"],
		"slug": first["slug"],
		"title": first["title"],
	}
	assert demoted_first["status"] == "ready"
	assert demoted_first["status_reason"] is None
	assert first_chunks["items"][0]["id"] == first_chunk["id"]
	assert first_chunks["items"][0]["status"] == "pending"


def test_starting_a_task_alone_reports_no_demotion(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("only", "Only")

	started = store.task_start(task["id"])

	assert started["demoted_task"] is None


def test_demoting_a_task_preserves_its_completed_chunks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = store.task_add("first", "First")
	completed_chunk = store.chunk_add(first["id"], "Done chunk")
	remaining_chunk = store.chunk_add(first["id"], "Remaining chunk")
	second = store.task_add("second", "Second")
	store.task_start(first["id"])
	store.chunk_complete(completed_chunk["id"])

	store.task_start(second["id"])
	first_chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		first["id"]
	)
	chunks_by_id = {chunk["id"]: chunk for chunk in first_chunks["items"]}

	assert chunks_by_id[completed_chunk["id"]]["status"] == "done"
	assert chunks_by_id[remaining_chunk["id"]]["status"] == "pending"


def test_blocking_returns_active_chunk_to_pending(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("blocked", "Blocked")
	chunk = store.chunk_add(task["id"], "Chunk")
	store.task_start(task["id"])

	blocked = store.task_block(task["id"], "Waiting for a decision")
	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert blocked["status"] == "blocked"
	assert chunks["items"][0]["status"] == "pending"
	assert chunks["items"][0]["id"] == chunk["id"]


def test_late_unfinished_dependency_blocks_ready_and_rejects_active(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("task", "Task")
	dependency = store.task_add("dependency", "Dependency")

	blocked = store.task_dependency_add(task["id"], dependency["id"])

	assert blocked["status"] == "blocked"
	with pytest.raises(InvalidTransitionError):
		store.task_start(task["id"])

	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	store.task_unblock(task["id"])
	store.task_start(task["id"])
	second_dependency = store.task_add("second-dependency", "Second dependency")

	with pytest.raises(InvalidDependencyError):
		store.task_dependency_add(task["id"], second_dependency["id"])


def test_notes_and_context_replace_the_project_context_row(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("notes", "Notes")
	discovery = store.discovery_add(task["id"], "The schema is shared.")
	decision = store.decision_add(
		task["id"], "Keep one context row.", supersedes_id=discovery["id"]
	)
	context = store.context_set(
		current_goal="Finish Commit 3",
		next_step="Run tests",
		verify_with="test:unit",
	)
	updated_context = store.context_set(current_goal="Finish Commit 4")

	assert discovery["id"].startswith("nte_")
	assert decision["supersedes_id"] == discovery["id"]
	assert context["next_step"] == "Run tests"
	assert updated_context["current_goal"] == "Finish Commit 4"
	assert updated_context["next_step"] is None


def test_remove_rejects_every_referenced_parent_row(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release")
	task = store.task_add("task", "Task", release_id=release["id"])
	chunk = store.chunk_add(task["id"], "Chunk")
	dependent = store.task_add("dependent", "Dependent")
	dependency = store.task_add("dependency", "Dependency")
	store.task_dependency_add(dependent["id"], task["id"])
	store.task_dependency_add(task["id"], dependency["id"])
	discovery = store.discovery_add(task["id"], "Discovery")

	with pytest.raises(StillReferencedError, match=task["id"]):
		store.release_remove(release["id"])

	with pytest.raises(StillReferencedError) as error:
		store.task_remove(task["id"])

	message = str(error.value)
	assert chunk["id"] in message
	assert f"{dependent['id']} -> {task['id']}" in message
	assert f"{task['id']} -> {dependency['id']}" in message
	assert discovery["id"] in message
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM releases WHERE id = ?", (release["id"],)
			).fetchone()
			is not None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM tasks WHERE id = ?", (task["id"],)
			).fetchone()
			is not None
		)


def test_remove_note_rejects_a_superseded_note(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("task", "Task")
	discovery = store.discovery_add(task["id"], "Discovery")
	decision = store.decision_add(task["id"], "Decision", supersedes_id=discovery["id"])

	with pytest.raises(StillReferencedError, match=decision["id"]):
		store.discovery_remove(discovery["id"])

	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM notes WHERE id = ?", (discovery["id"],)
			).fetchone()
			is not None
		)

	assert store.decision_remove(decision["id"]) == {"id": decision["id"]}
	assert store.discovery_remove(discovery["id"]) == {"id": discovery["id"]}


def test_remove_childless_rows_and_dependency_edges(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release")
	task = store.task_add("task", "Task", release_id=release["id"])
	dependency = store.task_add("dependency", "Dependency")
	chunk = store.chunk_add(task["id"], "Chunk")
	discovery = store.discovery_add(task["id"], "Discovery")
	store.task_dependency_add(task["id"], dependency["id"])

	assert store.task_dependency_remove(task["id"], dependency["id"]) == {
		"task_id": task["id"],
		"depends_on_task_id": dependency["id"],
	}
	assert store.chunk_remove(chunk["id"]) == {"id": chunk["id"]}
	assert store.discovery_remove(discovery["id"]) == {"id": discovery["id"]}
	assert store.task_remove(task["id"]) == {"id": task["id"]}
	assert store.task_remove(dependency["id"]) == {"id": dependency["id"]}
	assert store.release_remove(release["id"]) == {"id": release["id"]}

	with store.database.connection() as connection:
		for table, object_id in (
			("releases", release["id"]),
			("tasks", task["id"]),
			("tasks", dependency["id"]),
			("chunks", chunk["id"]),
			("notes", discovery["id"]),
		):
			assert (
				connection.execute(
					f"SELECT 1 FROM {table} WHERE id = ?", (object_id,)
				).fetchone()
				is None
			)


def test_rename_updates_titles_without_changing_identifiers_or_slugs(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release")
	task = store.task_add("task", "Task", release_id=release["id"])
	chunk = store.chunk_add(task["id"], "Chunk")

	renamed_release = store.release_rename(release["id"], "Renamed release")
	renamed_task = store.task_rename(task["id"], "Renamed task")
	renamed_chunk = store.chunk_rename(chunk["id"], "Renamed chunk")

	assert renamed_release["id"] == release["id"]
	assert renamed_release["slug"] == release["slug"]
	assert renamed_release["title"] == "Renamed release"
	assert renamed_task["id"] == task["id"]
	assert renamed_task["slug"] == task["slug"]
	assert renamed_task["title"] == "Renamed task"
	assert renamed_chunk["id"] == chunk["id"]
	assert renamed_chunk["task_id"] == chunk["task_id"]
	assert renamed_chunk["title"] == "Renamed chunk"


def test_task_edit_updates_selected_fields_and_preserves_lifecycle_data(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release")
	task = store.task_add(
		"task",
		"Task",
		overview="Original overview",
		purpose="Original purpose",
		contract="Original contract",
		model_tier="sonnet",
		files="original.py",
		acceptance_criteria="Original criteria",
		verification="Original verification",
		risks="Original risks",
		release_id=release["id"],
		position=3,
	)

	updated = store.task_edit(
		task["id"],
		overview="Updated overview",
		clear_model_tier=True,
		files="updated.py",
	)

	assert updated["id"] == task["id"]
	assert updated["project_id"] == task["project_id"]
	assert updated["slug"] == task["slug"]
	assert updated["release_id"] == task["release_id"]
	assert updated["title"] == task["title"]
	assert updated["overview"] == "Updated overview"
	assert updated["purpose"] == task["purpose"]
	assert updated["contract"] == task["contract"]
	assert updated["model_tier"] is None
	assert updated["files"] == "updated.py"
	assert updated["acceptance_criteria"] == task["acceptance_criteria"]
	assert updated["verification"] == task["verification"]
	assert updated["risks"] == task["risks"]
	assert updated["status"] == task["status"]
	assert updated["status_reason"] == task["status_reason"]
	assert updated["position"] == task["position"]
	assert updated["created_at"] == task["created_at"]
	assert updated["started_at"] == task["started_at"]
	assert updated["completed_at"] == task["completed_at"]
	assert updated["updated_at"] == task["updated_at"]


def test_task_edit_validates_all_values_before_writing(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("task", "Task", purpose="Original purpose")

	with pytest.raises(ProgressError, match="must be text"):
		store.task_edit(task["id"], overview="Updated overview", purpose=object())  # type: ignore[arg-type]

	current = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		task["id"]
	)

	assert current == task


def test_task_edit_requires_at_least_one_field(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("task", "Task")

	with pytest.raises(ProgressError, match="requires at least one field"):
		store.task_edit(task["id"])


def test_task_edit_rejects_value_and_clear_for_the_same_field(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("task", "Task")

	with pytest.raises(ProgressError, match="not both"):
		store.task_edit(task["id"], overview="Updated overview", clear_overview=True)


def test_task_edit_clears_text_fields_to_empty_strings(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = store.task_add("task", "Task", overview="Original overview")

	updated = store.task_edit(task["id"], clear_overview=True)

	assert updated["overview"] == ""


def test_release_edit_updates_only_overview_and_preserves_task_references(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add(
		"release",
		"Release",
		overview="Original overview",
		status="active",
		position=3,
	)
	tasks = [
		store.task_add(f"task-{number}", f"Task {number}", release_id=release["id"])
		for number in range(17)
	]

	updated = store.release_edit(release["id"], overview="Updated overview")

	assert updated["id"] == release["id"]
	assert updated["project_id"] == release["project_id"]
	assert updated["slug"] == release["slug"]
	assert updated["title"] == release["title"]
	assert updated["overview"] == "Updated overview"
	assert updated["status"] == release["status"]
	assert updated["position"] == release["position"]

	with store.database.connection() as connection:
		release_ids = [
			row["release_id"]
			for row in connection.execute(
				"SELECT release_id FROM tasks WHERE release_id = ? ORDER BY id",
				(release["id"],),
			).fetchall()
		]

	assert len(tasks) == 17
	assert release_ids == [release["id"]] * 17


@pytest.mark.parametrize(
	"edit_arguments",
	[
		pytest.param({"overview": ""}, id="empty-overview"),
		pytest.param({"clear_overview": True}, id="clear-flag"),
	],
)
def test_release_edit_can_clear_the_overview(
	tmp_path: Path, edit_arguments: dict[str, object]
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Overview")

	updated = store.release_edit(release["id"], **edit_arguments)

	assert updated["overview"] == ""


def test_release_edit_rejects_an_unchanged_overview(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Overview")

	with pytest.raises(ProgressError, match="already unchanged"):
		store.release_edit(release["id"], overview="Overview")


def test_release_edit_requires_one_overview_input(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release")

	with pytest.raises(ProgressError, match="requires"):
		store.release_edit(release["id"])


@pytest.mark.parametrize("initial_status", ["planned", "active"])
def test_release_complete_moves_planned_or_active_releases_to_done(
	tmp_path: Path, initial_status: str
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", status=initial_status)

	completed = store.release_complete(release["id"])

	assert completed["id"] == release["id"]
	assert completed["status"] == "done"


def test_release_complete_rejects_an_already_done_release(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", status="done")

	with pytest.raises(InvalidTransitionError, match="done"):
		store.release_complete(release["id"])

	assert (
		ReadStore(store.database, _ProjectStore(store.database)).release_list()[
			"items"
		][0]["status"]
		== "done"
	)


def test_two_short_writes_complete_with_the_configured_database_locking(
	tmp_path: Path,
) -> None:
	database = Database(tmp_path / "progress.db")
	with database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(PROJECT_ID, "agents", "Agent configuration", "2026-01-01T00:00:00+00:00"),
		)

	def add_release(number: int) -> dict[str, object]:
		return WriteStore(database, _ProjectStore(database)).release_add(
			f"release-{number}", f"Release {number}"
		)

	with ThreadPoolExecutor(max_workers=2) as executor:
		results = list(executor.map(add_release, (1, 2)))

	assert {result["slug"] for result in results} == {"release-1", "release-2"}


def test_two_process_writes_and_a_held_lock_preserve_database_integrity(
	tmp_path: Path, monkeypatch
) -> None:
	database_path = tmp_path / "progress.db"
	database = Database(database_path)
	with database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(PROJECT_ID, "agents", "Agent configuration", "2026-01-01T00:00:00+00:00"),
		)

	context = get_context("fork")
	results = context.Queue()
	processes = [
		context.Process(
			target=_add_release_in_process,
			args=(str(database_path), f"process-release-{number}", results),
		)
		for number in (1, 2)
	]

	for process in processes:
		process.start()
	short_write_results = [results.get(timeout=10) for _ in processes]
	for process in processes:
		process.join(timeout=10)

	assert all(process.exitcode == 0 for process in processes)
	assert {result for kind, result in short_write_results if kind == "ok"} == {
		"process-release-1",
		"process-release-2",
	}

	monkeypatch.setattr(database_module, "BUSY_TIMEOUT_SECONDS", 0.2)
	locked_connection = sqlite3.connect(
		database_path,
		timeout=database_module.BUSY_TIMEOUT_SECONDS,
		isolation_level=None,
	)
	locked_connection.execute("BEGIN IMMEDIATE")
	try:
		busy_results = context.Queue()
		busy_process = context.Process(
			target=_add_release_in_process,
			args=(str(database_path), "blocked-release", busy_results),
		)
		busy_process.start()
		assert busy_results.get(timeout=5) == ("error", DatabaseBusyError.code)
		busy_process.join(timeout=5)
		assert busy_process.exitcode == 0
	finally:
		locked_connection.rollback()
		locked_connection.close()

	with sqlite3.connect(database_path) as connection:
		integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]

	assert integrity == "ok"
