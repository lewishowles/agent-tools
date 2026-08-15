from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
import sqlite3

import pytest

from agents_progress.database import Database
from agents_progress import database as database_module
from agents_progress.errors import (
	ConflictingInProgressError,
	DatabaseBusyError,
	InvalidDependencyError,
	InvalidTransitionError,
	PendingChunksError,
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


def test_starting_a_second_task_names_the_existing_task(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = store.task_add("first", "First")
	second = store.task_add("second", "Second")
	store.task_start(first["id"])

	with pytest.raises(ConflictingInProgressError, match=first["id"]):
		store.task_start(second["id"])


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
