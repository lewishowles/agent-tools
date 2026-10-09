import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from agents_progress import database as database_module
from agents_progress.cli import _render_human_output
from agents_progress.database import Database
from agents_progress.errors import (
	DatabaseBusyError,
	InvalidDependencyError,
	InvalidTransitionError,
	NotFoundError,
	PendingChunksError,
	ProgressError,
	StillReferencedError,
	WrongObjectIdTypeError,
)
from agents_progress.projects import Project, ProjectStore
from agents_progress.reads import ReadStore
from agents_progress.repository import GitRepository
from agents_progress.style import span as render_span
from agents_progress.worktrees import WorktreeStore
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


def _add_task(store: WriteStore, slug: str, title: str, **arguments):
	"""Create a valid test task with default planning text."""
	arguments.setdefault("overview", f"{title} overview")
	arguments.setdefault("contract", [f"{title} contract"])
	return store.task_add(slug, title, **arguments)


def _add_chunk(
	store: WriteStore,
	task_id: str,
	title: str,
	description: str = "Chunk description",
	review_question: str = "Does the chunk do its one job?",
	**arguments,
):
	"""Create a valid test chunk with a default description and review question."""
	return store.chunk_add(
		task_id,
		title,
		description=description,
		review_question=review_question,
		**arguments,
	)


def _git_backed_completion_store(
	repository: Path,
) -> tuple[WriteStore, WorktreeStore]:
	"""Bind the shared repository for worktree cleanup tests."""
	database = Database(repository.parent / "progress.db")
	ProjectStore(database).init("agents", "Agent configuration", repository)
	return WriteStore(database), WorktreeStore(database)


@pytest.mark.parametrize(
	"arguments",
	[
		pytest.param({"overview": ""}, id="empty"),
		pytest.param({"overview": " \t"}, id="whitespace-only"),
	],
)
def test_task_add_rejects_a_blank_overview(
	tmp_path: Path, arguments: dict[str, str]
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(ProgressError, match="task overview"):
		store.task_add("task", "Task", **arguments)

	tasks = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert tasks["items"] == []


def test_task_add_requires_an_overview_argument(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(TypeError, match="overview"):
		store.task_add("task", "Task")

	tasks = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert tasks["items"] == []


@pytest.mark.parametrize(
	"arguments",
	[
		pytest.param({"contract": [""]}, id="empty-contract"),
		pytest.param({"contract": [" \t"]}, id="whitespace-contract"),
	],
)
def test_task_add_rejects_blank_contract(
	tmp_path: Path, arguments: dict[str, object]
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(ProgressError, match="task contract"):
		store.task_add(
			"task",
			"Task",
			overview="Task overview",
			contract=arguments.get("contract", ["Task contract"]),
		)

	tasks = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert tasks["items"] == []


@pytest.mark.parametrize("field", ["contract", "files"])
def test_task_add_rejects_scalar_task_values(tmp_path: Path, field: str) -> None:
	store = _seed_store(tmp_path)
	arguments = {"contract": ["Task contract"], "files": None}
	arguments[field] = "Task value"

	with pytest.raises(ProgressError, match="must be a list of text"):
		store.task_add(
			"task",
			"Task",
			overview="Task overview",
			**arguments,
		)

	tasks = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert tasks["items"] == []


@pytest.mark.parametrize(
	"overview",
	[
		pytest.param("", id="empty"),
		pytest.param(" \t", id="whitespace-only"),
	],
)
def test_release_add_rejects_a_blank_overview(tmp_path: Path, overview: str) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(ProgressError, match="release overview"):
		store.release_add("release", "Release", overview=overview)

	releases = ReadStore(store.database, _ProjectStore(store.database)).release_list()

	assert releases["items"] == []


def test_release_add_stores_overview_status_and_position(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)

	release = store.release_add(
		"release",
		"Release",
		overview="Release overview",
	)

	assert release == {
		"id": release["id"],
		"project_id": release["project_id"],
		"slug": "release",
		"title": "Release",
		"overview": "Release overview",
		"status": "planned",
		"position": 1,
	}


@pytest.mark.parametrize(
	"arguments",
	[
		pytest.param({"description": ""}, id="empty"),
		pytest.param({"description": " \t"}, id="whitespace-only"),
	],
)
def test_chunk_add_rejects_a_blank_description(
	tmp_path: Path, arguments: dict[str, str]
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")

	with pytest.raises(ProgressError, match="chunk description"):
		store.chunk_add(
			task["id"], "Chunk", review_question="Does it work?", **arguments
		)

	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert chunks["items"] == []


def test_chunk_add_requires_a_description_argument(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")

	with pytest.raises(TypeError, match="description"):
		store.chunk_add(task["id"], "Chunk")

	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert chunks["items"] == []


def _add_release_in_process(database_path: str, slug: str, results) -> None:
	try:
		database = Database(database_path)
		result = WriteStore(database, _ProjectStore(database)).release_add(
			slug, slug.title(), overview=f"{slug} overview"
		)
	# Catch every error so the parent process hears about it; an exception left in the child would be lost.
	except Exception as error:  # noqa: BLE001
		results.put(("error", getattr(error, "code", type(error).__name__)))
	else:
		results.put(("ok", result["slug"]))


def test_chunk_add_renumbers_an_explicit_zero_position_from_one(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add(
		"zero-release", "Zero release", overview="Zero release overview", position=0
	)
	task = _add_task(
		store, "zero-task", "Zero task", release_id=release["id"], position=0
	)
	chunk = _add_chunk(store, task["id"], "Zero chunk", position=0)

	assert release["position"] == 0
	assert task["position"] == 1
	assert chunk["position"] == 1


def test_task_add_without_position_goes_last_after_a_removal_in_each_queue(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	first = _add_task(store, "first", "First", release_id=release["id"])
	release_gap = _add_task(
		store, "release-gap", "Release gap", release_id=release["id"]
	)
	third = _add_task(store, "third", "Third", release_id=release["id"])
	unassigned_first = _add_task(store, "unassigned-first", "Unassigned first")
	unassigned_gap = _add_task(store, "unassigned-gap", "Unassigned gap")
	unassigned_third = _add_task(store, "unassigned-third", "Unassigned third")
	store.task_remove(release_gap["id"])
	store.task_remove(unassigned_gap["id"])

	release_last = _add_task(
		store, "release-last", "Release last", release_id=release["id"]
	)
	unassigned_last = _add_task(store, "unassigned-last", "Unassigned last")
	items = ReadStore(store.database, _ProjectStore(store.database)).task_list(
		show_all=True
	)["items"]
	release_items = [item for item in items if item["release_id"] == release["id"]]
	unassigned_items = [item for item in items if item["release_id"] is None]

	assert release_last["position"] == 3
	assert unassigned_last["position"] == 3
	assert [item["id"] for item in release_items] == [
		first["id"],
		third["id"],
		release_last["id"],
	]
	assert [item["position"] for item in release_items] == [1, 2, 3]
	assert [item["id"] for item in unassigned_items] == [
		unassigned_first["id"],
		unassigned_third["id"],
		unassigned_last["id"],
	]
	assert [item["position"] for item in unassigned_items] == [1, 2, 3]


def test_task_add_at_an_occupied_position_reorders_only_its_queue(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	other_release = store.release_add(
		"other-release", "Other release", overview="Other release overview"
	)
	first = _add_task(store, "first", "First", release_id=release["id"])
	second = _add_task(store, "second", "Second", release_id=release["id"])
	third = _add_task(store, "third", "Third", release_id=release["id"])
	store.task_complete(second["id"])
	other = _add_task(store, "other", "Other", release_id=other_release["id"])
	unassigned = _add_task(store, "unassigned", "Unassigned")

	inserted = _add_task(
		store, "inserted", "Inserted", release_id=release["id"], position=2
	)
	items = ReadStore(store.database, _ProjectStore(store.database)).task_list(
		show_all=True
	)["items"]
	release_items = [item for item in items if item["release_id"] == release["id"]]
	other_items = [item for item in items if item["release_id"] == other_release["id"]]
	unassigned_items = [item for item in items if item["release_id"] is None]

	assert inserted["position"] == 2
	assert [item["id"] for item in release_items] == [
		first["id"],
		inserted["id"],
		second["id"],
		third["id"],
	]
	assert [item["position"] for item in release_items] == [1, 2, 3, 4]
	assert [
		(item["slug"], item["title"], item["overview"], item["status"])
		for item in release_items
		if item["id"] != inserted["id"]
	] == [
		(task["slug"], task["title"], task["overview"], status)
		for task, status in ((first, "ready"), (second, "done"), (third, "ready"))
	]
	assert [item["id"] for item in other_items] == [other["id"]]
	assert [item["position"] for item in other_items] == [1]
	assert [item["id"] for item in unassigned_items] == [unassigned["id"]]
	assert [item["position"] for item in unassigned_items] == [1]


def test_task_add_at_position_one_moves_existing_tasks_down(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	second = _add_task(store, "second", "Second")
	third = _add_task(store, "third", "Third")

	inserted = _add_task(store, "inserted", "Inserted", position=1)
	items = ReadStore(store.database, _ProjectStore(store.database)).task_list()[
		"items"
	]

	assert inserted["position"] == 1
	assert [item["id"] for item in items] == [
		inserted["id"],
		first["id"],
		second["id"],
		third["id"],
	]
	assert [item["position"] for item in items] == [1, 2, 3, 4]


def test_task_add_places_a_position_past_the_end_last(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	second = _add_task(store, "second", "Second")

	last = _add_task(store, "last", "Last", position=99)
	items = ReadStore(store.database, _ProjectStore(store.database)).task_list()[
		"items"
	]

	assert last["position"] == 3
	assert [item["id"] for item in items] == [first["id"], second["id"], last["id"]]
	assert [item["position"] for item in items] == [1, 2, 3]


def test_task_add_at_an_occupied_position_closes_an_earlier_gap(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	second = _add_task(store, "second", "Second")
	third = _add_task(store, "third", "Third")
	fourth = _add_task(store, "fourth", "Fourth")
	store.task_remove(second["id"])

	inserted = _add_task(store, "inserted", "Inserted", position=4)
	items = ReadStore(store.database, _ProjectStore(store.database)).task_list()[
		"items"
	]

	assert inserted["position"] == 3
	assert [item["id"] for item in items] == [
		first["id"],
		third["id"],
		inserted["id"],
		fourth["id"],
	]
	assert [item["position"] for item in items] == [1, 2, 3, 4]


def test_task_move_reorders_only_the_task_queue(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	first = _add_task(store, "first", "First", release_id=release["id"])
	second = _add_task(store, "second", "Second", release_id=release["id"])
	third = _add_task(store, "third", "Third", release_id=release["id"])
	unassigned = _add_task(store, "unassigned", "Unassigned")

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
	first_release = store.release_add(
		"first-release", "First release", overview="First release overview"
	)
	second_release = store.release_add(
		"second-release", "Second release", overview="Second release overview"
	)
	first_task = _add_task(
		store, "first-task", "First task", release_id=first_release["id"]
	)
	second_task = _add_task(
		store, "second-task", "Second task", release_id=second_release["id"]
	)

	with pytest.raises(InvalidTransitionError, match="same release"):
		store.task_move(first_task["id"], before_task_id=second_task["id"])


def test_task_move_rehomes_and_unassigns_tasks_in_queue_order(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	source_release = store.release_add(
		"source-release", "Source release", overview="Source release overview"
	)
	target_release = store.release_add(
		"target-release", "Target release", overview="Target release overview"
	)
	source_first = _add_task(
		store, "source-first", "Source first", release_id=source_release["id"]
	)
	source_second = _add_task(
		store, "source-second", "Source second", release_id=source_release["id"]
	)
	target_first = _add_task(
		store, "target-first", "Target first", release_id=target_release["id"]
	)
	target_second = _add_task(
		store, "target-second", "Target second", release_id=target_release["id"]
	)
	unassigned_existing = _add_task(store, "unassigned-existing", "Unassigned existing")

	moved = store.task_move(source_first["id"], release_id=target_release["id"])
	ordered = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert moved["release_id"] == target_release["id"]
	assert moved["position"] == 3
	assert [
		item["id"]
		for item in ordered["items"]
		if item["release_id"] == target_release["id"]
	] == [target_first["id"], target_second["id"], source_first["id"]]

	precisely_moved = store.task_move(
		source_second["id"],
		release_id=target_release["id"],
		before_task_id=target_second["id"],
	)
	assert precisely_moved["position"] == 2
	ordered = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert [
		item["id"]
		for item in ordered["items"]
		if item["release_id"] == target_release["id"]
	] == [
		target_first["id"],
		source_second["id"],
		target_second["id"],
		source_first["id"],
	]

	unassigned = store.task_move(source_first["id"], release_id="")
	assert unassigned["release_id"] is None
	assert unassigned["position"] == 2
	ordered = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert [item["id"] for item in ordered["items"] if item["release_id"] is None] == [
		unassigned_existing["id"],
		source_first["id"],
	]


def test_task_move_reassigns_before_an_unassigned_task(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	assigned = _add_task(store, "assigned", "Assigned", release_id=release["id"])
	unassigned_first = _add_task(store, "unassigned-first", "Unassigned first")
	unassigned_second = _add_task(store, "unassigned-second", "Unassigned second")

	moved = store.task_move(
		assigned["id"],
		release_id="",
		before_task_id=unassigned_second["id"],
	)
	ordered = ReadStore(store.database, _ProjectStore(store.database)).task_list()

	assert moved["release_id"] is None
	assert moved["position"] == 2
	assert [item["id"] for item in ordered["items"] if item["release_id"] is None] == [
		unassigned_first["id"],
		assigned["id"],
		unassigned_second["id"],
	]


def test_task_move_preserves_children_notes_and_dependencies(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	source_release = store.release_add(
		"source-release", "Source release", overview="Source release overview"
	)
	target_release = store.release_add(
		"target-release", "Target release", overview="Target release overview"
	)
	dependency = _add_task(store, "dependency", "Dependency")
	task = _add_task(
		store,
		"task",
		"Task",
		release_id=source_release["id"],
		depends_on=(dependency["id"],),
	)
	chunk = _add_chunk(store, task["id"], "Chunk")
	discovery = store.discovery_add(task["id"], "Discovery")
	decision = store.decision_add(task["id"], "Decision", supersedes_id=discovery["id"])

	with store.database.connection() as connection:
		children_before = {
			"chunks": [
				tuple(row)
				for row in connection.execute(
					"SELECT id, task_id, position, title, description, status "
					"FROM chunks WHERE task_id = ?",
					(task["id"],),
				)
			],
			"notes": [
				tuple(row)
				for row in connection.execute(
					"SELECT id, task_id, type, body, supersedes_id FROM notes "
					"WHERE task_id = ? ORDER BY id",
					(task["id"],),
				)
			],
			"dependencies": [
				tuple(row)
				for row in connection.execute(
					"SELECT task_id, depends_on_task_id FROM task_dependencies "
					"WHERE task_id = ?",
					(task["id"],),
				)
			],
		}

	moved = store.task_move(task["id"], release_id=target_release["id"])

	assert moved["release_id"] == target_release["id"]
	assert chunk["id"] in {row[0] for row in children_before["chunks"]}
	assert discovery["id"] in {row[0] for row in children_before["notes"]}
	assert decision["id"] in {row[0] for row in children_before["notes"]}
	assert dependency["id"] == children_before["dependencies"][0][1]

	with store.database.connection() as connection:
		children_after = {
			"chunks": [
				tuple(row)
				for row in connection.execute(
					"SELECT id, task_id, position, title, description, status "
					"FROM chunks WHERE task_id = ?",
					(task["id"],),
				)
			],
			"notes": [
				tuple(row)
				for row in connection.execute(
					"SELECT id, task_id, type, body, supersedes_id FROM notes "
					"WHERE task_id = ? ORDER BY id",
					(task["id"],),
				)
			],
			"dependencies": [
				tuple(row)
				for row in connection.execute(
					"SELECT task_id, depends_on_task_id FROM task_dependencies "
					"WHERE task_id = ?",
					(task["id"],),
				)
			],
		}

	assert children_after == children_before


def test_task_move_validates_release_and_position_target(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	source_release = store.release_add(
		"source-release", "Source release", overview="Source release overview"
	)
	other_release = store.release_add(
		"other-release", "Other release", overview="Other release overview"
	)
	task = _add_task(store, "task", "Task", release_id=source_release["id"])
	target = _add_task(store, "target", "Target", release_id=other_release["id"])

	with pytest.raises(NotFoundError, match="release rel_"):
		store.task_move(task["id"], release_id="rel_" + "r" * 22)

	with pytest.raises(InvalidTransitionError, match="target release"):
		store.task_move(
			task["id"],
			release_id=source_release["id"],
			before_task_id=target["id"],
		)


def test_chunk_add_without_position_goes_last_after_a_removal(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-positions", "Chunk positions")
	first = _add_chunk(store, task["id"], "First")
	second = _add_chunk(store, task["id"], "Second")
	third = _add_chunk(store, task["id"], "Third")
	store.chunk_remove(second["id"])

	last = _add_chunk(store, task["id"], "Last")
	items = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)["items"]

	assert last["position"] == 3
	assert [item["id"] for item in items] == [first["id"], third["id"], last["id"]]
	assert [item["position"] for item in items] == [1, 2, 3]


def test_chunk_add_at_position_one_moves_existing_chunks_down(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-first", "Chunk first")
	first = _add_chunk(store, task["id"], "First")
	second = _add_chunk(store, task["id"], "Second")
	third = _add_chunk(store, task["id"], "Third")

	inserted = _add_chunk(store, task["id"], "Inserted", position=1)
	items = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)["items"]

	assert inserted["position"] == 1
	assert [item["id"] for item in items] == [
		inserted["id"],
		first["id"],
		second["id"],
		third["id"],
	]
	assert [item["position"] for item in items] == [1, 2, 3, 4]


def test_chunk_add_at_position_one_closes_a_gap_before_the_first_chunk(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-first-gap", "Chunk first gap")
	first = _add_chunk(store, task["id"], "First")
	second = _add_chunk(store, task["id"], "Second")
	third = _add_chunk(store, task["id"], "Third")
	store.chunk_remove(first["id"])

	inserted = _add_chunk(store, task["id"], "Inserted", position=1)
	items = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)["items"]

	assert inserted["position"] == 1
	assert [item["id"] for item in items] == [
		inserted["id"],
		second["id"],
		third["id"],
	]
	assert [item["position"] for item in items] == [1, 2, 3]


def test_chunk_add_shifts_chunks_at_and_after_an_occupied_position(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-insert", "Chunk insert")
	first = _add_chunk(store, task["id"], "First")
	second = _add_chunk(store, task["id"], "Second")
	third = _add_chunk(store, task["id"], "Third")
	other_task = _add_task(store, "other-task", "Other task")
	other_chunk = _add_chunk(store, other_task["id"], "Other chunk")

	inserted = _add_chunk(store, task["id"], "Inserted", position=2)
	items = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)["items"]
	other_items = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		other_task["id"]
	)["items"]

	assert inserted["position"] == 2
	assert [item["id"] for item in items] == [
		first["id"],
		inserted["id"],
		second["id"],
		third["id"],
	]
	assert [item["position"] for item in items] == [1, 2, 3, 4]
	assert [
		(item["title"], item["description"], item["review_question"], item["status"])
		for item in items
		if item["id"] != inserted["id"]
	] == [
		(
			chunk["title"],
			chunk["description"],
			chunk["review_question"],
			chunk["status"],
		)
		for chunk in (first, second, third)
	]
	assert [item["id"] for item in other_items] == [other_chunk["id"]]
	assert [item["position"] for item in other_items] == [1]


def test_chunk_add_at_an_occupied_position_closes_an_earlier_gap(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-gap", "Chunk gap")
	first = _add_chunk(store, task["id"], "First")
	second = _add_chunk(store, task["id"], "Second")
	third = _add_chunk(store, task["id"], "Third")
	fourth = _add_chunk(store, task["id"], "Fourth")
	store.chunk_remove(second["id"])

	inserted = _add_chunk(store, task["id"], "Inserted", position=4)
	items = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)["items"]

	assert inserted["position"] == 3
	assert [item["id"] for item in items] == [
		first["id"],
		third["id"],
		inserted["id"],
		fourth["id"],
	]
	assert [item["position"] for item in items] == [1, 2, 3, 4]


def test_chunk_add_places_a_position_past_the_end_last(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-append", "Chunk append")
	first = _add_chunk(store, task["id"], "First")
	second = _add_chunk(store, task["id"], "Second")

	last = _add_chunk(store, task["id"], "Last", position=99)
	items = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)["items"]

	assert last["position"] == 3
	assert [item["id"] for item in items] == [first["id"], second["id"], last["id"]]
	assert [item["position"] for item in items] == [1, 2, 3]


def test_chunk_move_reorders_only_the_task_chunks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-move", "Chunk move")
	first = _add_chunk(store, task["id"], "First")
	second = _add_chunk(store, task["id"], "Second")
	third = _add_chunk(store, task["id"], "Third")
	other_task = _add_task(store, "other-task", "Other task")
	other_chunk = _add_chunk(store, other_task["id"], "Other chunk")

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
	first_task = _add_task(store, "first-task", "First task")
	second_task = _add_task(store, "second-task", "Second task")
	first_chunk = _add_chunk(store, first_task["id"], "First chunk")
	second_chunk = _add_chunk(store, second_task["id"], "Second chunk")

	with pytest.raises(InvalidTransitionError, match="same task"):
		store.chunk_move(first_chunk["id"], before_chunk_id=second_chunk["id"])


def test_creation_and_chunk_lifecycle_are_atomic(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Progress store", "Store progress.")
	task = _add_task(
		store,
		"lifecycle",
		"Lifecycle",
		release_id=release["id"],
	)
	first_chunk = _add_chunk(store, task["id"], "First", "First chunk.")
	second_chunk = _add_chunk(store, task["id"], "Second", "Second chunk.")

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


@pytest.mark.parametrize("use_slug", [False, True])
def test_task_start_accepts_an_id_or_slug(tmp_path: Path, use_slug: bool) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "start-task", "Start task")
	reference = task["slug"] if use_slug else task["id"]

	started = store.task_start(reference)

	assert started["id"] == task["id"]
	assert started["status"] == "in-progress"


@pytest.mark.parametrize("reference", ["missing-task", "tsk_" + "t" * 22])
def test_task_start_rejects_an_unknown_identifier(
	tmp_path: Path, reference: str
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(NotFoundError):
		store.task_start(reference)


@pytest.mark.parametrize("method_name", ["task_start", "task_complete"])
def test_task_lifecycle_rejects_a_wrong_object_type(
	tmp_path: Path, method_name: str
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(WrongObjectIdTypeError):
		getattr(store, method_name)("chk_" + "c" * 22)


@pytest.mark.parametrize("use_slug", [False, True])
def test_task_complete_accepts_an_id_or_slug(tmp_path: Path, use_slug: bool) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "complete-task", "Complete task")
	store.task_start(task["id"])
	reference = task["slug"] if use_slug else task["id"]

	completed = store.task_complete(reference)

	assert completed["id"] == task["id"]
	assert completed["status"] == "done"


def test_task_complete_accepts_a_ready_task_without_starting_it(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "ready-task", "Ready task")

	completed = store.task_complete(task["id"])

	assert completed["status"] == "done"
	assert completed["started_at"] is None
	assert completed["completed_at"] is not None


def test_task_complete_removes_a_clean_managed_worktree(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "clean-task", "Clean task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])

	completed = store.task_complete(task["id"], path=committed_repository)

	assert completed["status"] == "done"
	assert completed["worktree_cleanup"] == {"status": "removed"}
	assert not checkout.exists()
	assert worktrees.cleanup(task["id"], committed_repository) == {"status": "none"}
	assert (
		GitRepository(committed_repository)
		.run(
			[
				"show-ref",
				"--verify",
				"--quiet",
				f"refs/heads/progress/task/{task['id']}",
			]
		)
		.returncode
		== 0
	)


def test_task_complete_reports_no_worktree_when_none_was_ensured(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "plain-task", "Plain task", path=committed_repository)

	completed = store.task_complete(task["id"], path=committed_repository)

	assert completed["worktree_cleanup"] == {"status": "none"}
	assert worktrees.cleanup(task["id"], committed_repository) == {"status": "none"}


@pytest.mark.parametrize("dirty_kind", ["modified", "untracked"])
def test_task_complete_keeps_a_dirty_worktree_and_retries_after_it_is_clean(
	tmp_path: Path,
	committed_repository: Path,
	dirty_kind: str,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	clean_task = _add_task(store, "clean-task", "Clean task", path=committed_repository)
	dirty_task = _add_task(store, "dirty-task", "Dirty task", path=committed_repository)
	clean_checkout = Path(
		worktrees.ensure(clean_task["id"], committed_repository)["path"]
	)
	dirty_checkout = Path(
		worktrees.ensure(dirty_task["id"], committed_repository)["path"]
	)
	if dirty_kind == "modified":
		(dirty_checkout / "tracked.txt").write_text("unfinished work\n")
	else:
		(dirty_checkout / "new-file.txt").write_text("unfinished work\n")

	completed = store.task_complete(
		[clean_task["id"], dirty_task["id"]], path=committed_repository
	)

	assert [task["status"] for task in completed] == ["done", "done"]
	assert completed[0]["worktree_cleanup"] == {"status": "removed"}
	assert completed[1]["worktree_cleanup"]["status"] == "pending"
	assert not clean_checkout.exists()
	assert dirty_checkout.is_dir()
	assert worktrees.get(dirty_task["id"], committed_repository)["path"] == str(
		dirty_checkout
	)

	if dirty_kind == "modified":
		(dirty_checkout / "tracked.txt").write_text("committed content\n")
	else:
		(dirty_checkout / "new-file.txt").rename(tmp_path / "saved-untracked.txt")

	assert worktrees.cleanup(dirty_task["id"], committed_repository) == {
		"status": "removed"
	}
	assert not dirty_checkout.exists()


def test_task_complete_keeps_a_checkout_when_git_refuses_removal(
	committed_repository: Path, monkeypatch
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "refused-task", "Refused task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	original_run = GitRepository.run

	def refuse_removal(self, arguments):
		"""Simulate a Git refusal after checkout ownership has been checked."""
		if arguments == ["worktree", "remove", str(checkout)]:
			return subprocess.CompletedProcess(arguments, 128, "", "checkout is locked")
		return original_run(self, arguments)

	with monkeypatch.context() as patch:
		patch.setattr(GitRepository, "run", refuse_removal)
		completed = store.task_complete(task["id"], path=committed_repository)

	assert completed["status"] == "done"
	assert completed["worktree_cleanup"] == {
		"status": "pending",
		"reason": "checkout is locked",
	}
	assert checkout.is_dir()
	assert worktrees.get(task["id"], committed_repository)["path"] == str(checkout)
	assert worktrees.cleanup(task["id"], committed_repository) == {"status": "removed"}


def test_task_complete_does_not_remove_the_current_checkout(
	committed_repository: Path, monkeypatch
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "current-task", "Current task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	monkeypatch.chdir(checkout)

	completed = store.task_complete(task["id"], path=checkout)

	assert completed["worktree_cleanup"] == {
		"status": "pending",
		"reason": "command uses the task worktree",
	}
	assert checkout.is_dir()
	assert worktrees.get(task["id"], committed_repository)["path"] == str(checkout)
	assert worktrees.cleanup(task["id"], committed_repository)["status"] == "pending"


def test_task_complete_does_not_remove_a_checkout_named_as_its_git_path(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "selected-task", "Selected task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])

	completed = store.task_complete(task["id"], path=checkout)

	assert completed["worktree_cleanup"] == {
		"status": "pending",
		"reason": "command uses the task worktree",
	}
	assert checkout.is_dir()
	assert worktrees.get(task["id"], committed_repository)["path"] == str(checkout)


def test_task_complete_continues_cleanup_after_an_os_error(
	committed_repository: Path, monkeypatch
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	first = _add_task(store, "first-task", "First task", path=committed_repository)
	second = _add_task(store, "second-task", "Second task", path=committed_repository)
	first_checkout = Path(worktrees.ensure(first["id"], committed_repository)["path"])
	second_checkout = Path(worktrees.ensure(second["id"], committed_repository)["path"])
	original_run = GitRepository.run

	def fail_first_removal(self, arguments):
		"""Fail the first Git removal without changing the second checkout."""
		if arguments == ["worktree", "remove", str(first_checkout)]:
			raise OSError("Git process unavailable")
		return original_run(self, arguments)

	with monkeypatch.context() as patch:
		patch.setattr(GitRepository, "run", fail_first_removal)
		completed = store.task_complete(
			[first["id"], second["id"]], path=committed_repository
		)

	assert [task["status"] for task in completed] == ["done", "done"]
	assert completed[0]["worktree_cleanup"] == {
		"status": "pending",
		"reason": "Git process unavailable",
	}
	assert completed[1]["worktree_cleanup"] == {"status": "removed"}
	assert first_checkout.is_dir()
	assert not second_checkout.exists()
	assert worktrees.get(first["id"], committed_repository)["path"] == str(
		first_checkout
	)


def test_task_complete_clears_a_record_when_the_worktree_folder_is_gone(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "missing-task", "Missing task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	shutil.rmtree(checkout)
	assert (
		str(checkout)
		in GitRepository(committed_repository)
		.run(["worktree", "list", "--porcelain"])
		.stdout
	)

	completed = store.task_complete(task["id"], path=committed_repository)

	assert completed["worktree_cleanup"] == {
		"status": "removed",
		"reason": "folder already gone",
	}
	assert worktrees.cleanup(task["id"], committed_repository) == {"status": "none"}
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_worktree_cleanup_refuses_a_task_that_is_not_done(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "ready-task", "Ready task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])

	with pytest.raises(InvalidTransitionError, match="must be done"):
		worktrees.cleanup(task["id"], committed_repository)

	assert checkout.is_dir()


@pytest.mark.parametrize("status", ["blocked", "needs-decision", "done"])
def test_task_complete_rejects_non_ready_tasks(tmp_path: Path, status: str) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, f"{status}-task", f"{status.title()} task")

	if status == "done":
		store.task_complete(task["id"])
	else:
		store.task_block(
			task["id"], "Waiting for input", needs_decision=status == "needs-decision"
		)

	with pytest.raises(InvalidTransitionError, match="ready or in progress"):
		store.task_complete(task["id"])


@pytest.mark.parametrize("reference", ["missing-task", "tsk_" + "t" * 22])
def test_task_complete_rejects_an_unknown_identifier(
	tmp_path: Path, reference: str
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(NotFoundError):
		store.task_complete(reference)


def test_chunk_start_demotes_active_chunk_and_activates_target(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-start", "Chunk start")
	active_chunk = _add_chunk(store, task["id"], "Active chunk")
	pending_chunk = _add_chunk(store, task["id"], "Pending chunk")
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
	task = _add_task(store, "ready-task", "Ready task")
	chunk = _add_chunk(store, task["id"], "Pending chunk")

	with pytest.raises(InvalidTransitionError, match="progress task start"):
		store.chunk_start(chunk["id"])


@pytest.mark.parametrize("status", ["active", "done", "skipped"])
def test_chunk_start_rejects_non_pending_chunks(tmp_path: Path, status: str) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, f"{status}-task", f"{status.title()} task")
	chunk = _add_chunk(store, task["id"], "Chunk")
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


def test_chunk_complete_accepts_a_pending_chunk(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "pending-chunk-task", "Pending chunk task")
	chunk = _add_chunk(store, task["id"], "Pending chunk")

	completed = store.chunk_complete(chunk["id"])

	assert completed["status"] == "done"
	assert completed["started_at"] is None
	assert completed["completed_at"] is not None


def test_chunk_complete_leaves_an_earlier_pending_sibling_unchanged(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "ready-task", "Ready task")
	earlier_chunk = _add_chunk(store, task["id"], "Earlier chunk")
	later_chunk = _add_chunk(store, task["id"], "Later chunk")

	completed = store.chunk_complete(later_chunk["id"])
	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)
	chunks_by_id = {chunk["id"]: chunk for chunk in chunks["items"]}

	assert completed["status"] == "done"
	assert chunks_by_id[earlier_chunk["id"]]["status"] == "pending"
	assert chunks_by_id[later_chunk["id"]]["status"] == "done"
	assert chunks_by_id[earlier_chunk["id"]]["started_at"] is None


def test_chunk_complete_leaves_an_active_sibling_unchanged(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "in-progress-task", "In-progress task")
	active_chunk = _add_chunk(store, task["id"], "Active chunk")
	pending_chunk = _add_chunk(store, task["id"], "Pending chunk")
	later_chunk = _add_chunk(store, task["id"], "Later chunk")
	store.task_start(task["id"])

	completed = store.chunk_complete(later_chunk["id"])
	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)
	chunks_by_id = {chunk["id"]: chunk for chunk in chunks["items"]}

	assert completed["status"] == "done"
	assert chunks_by_id[active_chunk["id"]]["status"] == "active"
	assert chunks_by_id[pending_chunk["id"]]["status"] == "pending"
	assert chunks_by_id[later_chunk["id"]]["status"] == "done"


def test_chunk_complete_applies_earlier_completion_to_a_later_id(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "ordered-chunks-task", "Ordered chunks task")
	active_chunk = _add_chunk(store, task["id"], "Active chunk")
	next_pending_chunk = _add_chunk(store, task["id"], "Next pending chunk")
	store.task_start(task["id"])

	completed = store.chunk_complete([active_chunk["id"], next_pending_chunk["id"]])

	assert [result["id"] for result in completed] == [
		active_chunk["id"],
		next_pending_chunk["id"],
	]
	assert [result["status"] for result in completed] == ["done", "done"]
	assert completed[1]["started_at"] is not None


@pytest.mark.parametrize("status", ["done", "skipped"])
def test_chunk_complete_rejects_finished_chunks(tmp_path: Path, status: str) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, f"{status}-chunk-task", f"{status.title()} chunk task")
	chunk = _add_chunk(store, task["id"], "Chunk")

	if status == "done":
		store.chunk_complete(chunk["id"])
	else:
		with store.database.transaction() as connection:
			connection.execute(
				"UPDATE chunks SET status = 'skipped' WHERE id = ?", (chunk["id"],)
			)

	with pytest.raises(InvalidTransitionError, match="pending or active"):
		store.chunk_complete(chunk["id"])


def test_dependencies_block_and_complete_tasks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	dependency = _add_task(store, "dependency", "Dependency")
	dependent = _add_task(
		store, "dependent", "Dependent", depends_on=[dependency["id"]]
	)

	assert dependent["status"] == "waiting"
	assert "unresolved dependencies" in dependent["status_reason"]
	with pytest.raises(InvalidTransitionError):
		store.task_unblock(dependent["id"])

	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	ready_dependent = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		dependent["id"]
	)

	assert ready_dependent["status"] == "ready"
	assert ready_dependent["status_reason"] is None


def test_task_block_changes_a_waiting_task_to_a_manual_block(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	dependency = _add_task(store, "dependency", "Dependency")
	waiting = _add_task(store, "waiting", "Waiting", depends_on=[dependency["id"]])

	blocked = store.task_block(waiting["id"], "Needs a decision")

	assert blocked["status"] == "blocked"
	assert blocked["status_reason"] == "Needs a decision"


def test_task_unblock_rejects_a_waiting_task(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	dependency = _add_task(store, "dependency", "Dependency")
	waiting = _add_task(store, "waiting", "Waiting", depends_on=[dependency["id"]])

	with pytest.raises(InvalidTransitionError, match="is not blocked"):
		store.task_unblock(waiting["id"])

	assert (
		ReadStore(store.database, _ProjectStore(store.database)).task_get(
			waiting["id"]
		)["status"]
		== "waiting"
	)


def test_task_complete_unblocks_dependents_and_reports_them(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	dependency = _add_task(
		store, "progress-cli-release-edit", "Release edit dependency"
	)
	dependent = _add_task(
		store,
		"progress-cli-task-chunk-edit",
		"Task chunk edit",
		depends_on=[dependency["id"]],
	)
	store.task_start(dependency["id"])

	completed = store.task_complete(dependency["id"])
	ready_dependent = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		dependent["id"]
	)

	assert completed["unblocked_tasks"] == [
		{
			"id": dependent["id"],
			"slug": dependent["slug"],
			"title": dependent["title"],
		}
	]
	assert ready_dependent["status"] == "ready"
	assert ready_dependent["status_reason"] is None


def test_removing_the_last_dependency_readies_only_waiting_tasks(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	dependency = _add_task(store, "dependency", "Dependency")
	waiting = _add_task(store, "waiting", "Waiting", depends_on=[dependency["id"]])
	blocked = _add_task(store, "blocked", "Blocked", depends_on=[dependency["id"]])
	store.task_block(blocked["id"], "Needs a decision")

	store.task_dependency_remove(waiting["id"], dependency["id"])
	store.task_dependency_remove(blocked["id"], dependency["id"])
	reads = ReadStore(store.database, _ProjectStore(store.database))

	assert reads.task_get(waiting["id"])["status"] == "ready"
	assert reads.task_get(blocked["id"])["status"] == "blocked"


def test_completing_a_dependency_keeps_manual_blocks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	dependency = _add_task(store, "dependency", "Dependency")
	blocked = _add_task(store, "blocked", "Blocked", depends_on=[dependency["id"]])
	store.task_block(blocked["id"], "Needs a decision")

	store.task_complete(dependency["id"])
	reads = ReadStore(store.database, _ProjectStore(store.database))

	assert reads.task_get(blocked["id"])["status"] == "blocked"
	assert store.task_unblock(blocked["id"])["status"] == "ready"


def test_task_complete_applies_earlier_completion_to_a_later_id(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	dependency = _add_task(store, "ordered-dependency", "Ordered dependency")
	dependent = _add_task(
		store,
		"ordered-dependent",
		"Ordered dependent",
		depends_on=[dependency["id"]],
	)

	completed = store.task_complete([dependency["id"], dependent["id"]])

	assert [result["id"] for result in completed] == [
		dependency["id"],
		dependent["id"],
	]
	assert [result["status"] for result in completed] == ["done", "done"]


def test_task_complete_keeps_dependents_blocked_with_incomplete_dependencies(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	completed_dependency = _add_task(
		store, "completed-dependency", "Completed dependency"
	)
	unfinished_dependency = _add_task(
		store, "unfinished-dependency", "Unfinished dependency"
	)
	dependent = _add_task(
		store,
		"dependent",
		"Dependent",
		depends_on=[completed_dependency["id"], unfinished_dependency["id"]],
	)
	status_reason = dependent["status_reason"]
	store.task_start(completed_dependency["id"])

	completed = store.task_complete(completed_dependency["id"])
	blocked_dependent = ReadStore(
		store.database, _ProjectStore(store.database)
	).task_get(dependent["id"])

	assert completed["unblocked_tasks"] == []
	assert blocked_dependent["status"] == "waiting"
	assert blocked_dependent["status_reason"] == status_reason


@pytest.mark.parametrize("start_second_first", [False, True])
def test_starting_two_tasks_keeps_their_active_chunks(
	tmp_path: Path, start_second_first: bool
) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	first_chunk = _add_chunk(store, first["id"], "First chunk")
	second = _add_task(store, "second", "Second")
	second_chunk = _add_chunk(store, second["id"], "Second chunk")
	ordered_tasks = (second, first) if start_second_first else (first, second)
	started_first = store.task_start(ordered_tasks[0]["id"])
	store.task_start(ordered_tasks[1]["id"], secondary=True)
	reader = ReadStore(store.database, _ProjectStore(store.database))

	assert "demoted_task" not in started_first
	for task, chunk in ((first, first_chunk), (second, second_chunk)):
		assert reader.task_get(task["id"])["status"] == "in-progress"
		assert reader.chunk_get(chunk["id"])["status"] == "active"
		assert reader.chunk_get(chunk["id"])["started_at"] is not None


def test_plain_start_requires_secondary_when_a_default_is_active(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	second = _add_task(store, "second", "Second")
	store.task_start(first["id"])

	with pytest.raises(InvalidTransitionError, match="--secondary") as error:
		store.task_start(second["id"])

	assert f"task {first['id']} is already the project's default task" in str(
		error.value
	)
	assert f"run task {second['id']} alongside it" in str(error.value)

	reader = ReadStore(store.database, _ProjectStore(store.database))
	assert reader.task_get(second["id"])["status"] == "ready"
	assert reader.next()["task"]["id"] == first["id"]


def test_secondary_start_does_not_claim_an_empty_default(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	secondary = _add_task(store, "secondary", "Secondary")
	default = _add_task(store, "default", "Default")
	store.task_start(secondary["id"], secondary=True)
	reader = ReadStore(store.database, _ProjectStore(store.database))

	assert reader.next()["task"]["id"] == default["id"]
	store.task_start(default["id"])
	assert reader.next()["task"]["id"] == default["id"]


@pytest.mark.parametrize("complete_second", [False, True])
def test_completing_either_task_leaves_the_other_active(
	tmp_path: Path, complete_second: bool
) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	first_chunk = _add_chunk(store, first["id"], "First chunk")
	second = _add_task(store, "second", "Second")
	second_chunk = _add_chunk(store, second["id"], "Second chunk")
	store.task_start(first["id"])
	store.task_start(second["id"], secondary=True)
	completed_task, completed_chunk = (
		(second, second_chunk) if complete_second else (first, first_chunk)
	)
	other_task, other_chunk = (
		(first, first_chunk) if complete_second else (second, second_chunk)
	)
	reader = ReadStore(store.database, _ProjectStore(store.database))
	other_before = reader.task_get(other_task["id"])
	other_chunk_before = reader.chunk_get(other_chunk["id"])

	store.chunk_complete(completed_chunk["id"])
	store.task_complete(completed_task["id"])

	assert reader.task_get(completed_task["id"])["status"] == "done"
	assert reader.task_get(other_task["id"]) == other_before
	assert reader.chunk_get(other_chunk["id"]) == other_chunk_before
	selected = reader.next()["task"]
	assert (selected["id"] if selected is not None else None) == (
		first["id"] if complete_second else None
	)


@pytest.mark.parametrize("complete_secondary_first", [False, True])
def test_completing_tasks_clears_only_their_handoffs(
	tmp_path: Path, complete_secondary_first: bool
) -> None:
	store = _seed_store(tmp_path)
	default = _add_task(store, "default", "Default")
	secondary = _add_task(store, "secondary", "Secondary")
	store.context_set(current_goal="Project planning")
	store.task_start(default["id"])
	store.task_start(secondary["id"], secondary=True)
	store.context_set(task_id=default["id"], current_goal="Default work")
	store.context_set(task_id=secondary["id"], current_goal="Secondary work")
	reader = ReadStore(store.database, _ProjectStore(store.database))
	completed, remaining = (
		(secondary, default) if complete_secondary_first else (default, secondary)
	)
	remaining_goal = "Default work" if complete_secondary_first else "Secondary work"

	store.task_complete(completed["id"])

	assert reader.context_get(task_id=completed["id"])["status"] == "not-set"
	assert reader.context_get(task_id=remaining["id"])["current_goal"] == (
		remaining_goal
	)
	assert reader.context_get()["current_goal"] == (
		"Default work" if complete_secondary_first else "Project planning"
	)
	with store.database.connection() as connection:
		project_handoff = connection.execute(
			"SELECT current_goal FROM context WHERE project_id = ?", (PROJECT_ID,)
		).fetchone()
	assert project_handoff["current_goal"] == "Project planning"

	store.task_complete(remaining["id"])

	assert reader.context_get(task_id=remaining["id"])["status"] == "not-set"
	assert reader.context_get()["current_goal"] == "Project planning"


def test_completing_several_tasks_clears_each_handoff(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	default = _add_task(store, "default", "Default")
	secondary = _add_task(store, "secondary", "Secondary")
	store.context_set(current_goal="Project planning")
	store.task_start(default["id"])
	store.task_start(secondary["id"], secondary=True)
	store.context_set(task_id=default["id"], current_goal="Default work")
	store.context_set(task_id=secondary["id"], current_goal="Secondary work")
	reader = ReadStore(store.database, _ProjectStore(store.database))

	store.task_complete([default["id"], secondary["id"]])

	assert reader.context_get(task_id=default["id"])["status"] == "not-set"
	assert reader.context_get(task_id=secondary["id"])["status"] == "not-set"
	assert reader.context_get()["current_goal"] == "Project planning"


def test_a_waiting_task_cannot_start_while_another_task_is_active(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	active = _add_task(store, "active", "Active")
	active_chunk = _add_chunk(store, active["id"], "Active chunk")
	dependency = _add_task(store, "dependency", "Dependency")
	dependent = _add_task(
		store, "dependent", "Dependent", depends_on=[dependency["id"]]
	)
	store.task_start(active["id"])
	reader = ReadStore(store.database, _ProjectStore(store.database))
	active_before = reader.task_get(active["id"])
	chunk_before = reader.chunk_get(active_chunk["id"])

	with pytest.raises(InvalidTransitionError, match="must be ready"):
		store.task_start(dependent["id"])

	assert reader.task_get(active["id"]) == active_before
	assert reader.chunk_get(active_chunk["id"]) == chunk_before


def test_blocking_returns_active_chunk_to_pending(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "blocked", "Blocked")
	chunk = _add_chunk(store, task["id"], "Chunk")
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
	task = _add_task(store, "task", "Task")
	dependency = _add_task(store, "dependency", "Dependency")

	blocked = store.task_dependency_add(task["id"], dependency["id"])

	assert blocked["status"] == "waiting"
	with pytest.raises(InvalidTransitionError):
		store.task_start(task["id"])

	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	store.task_start(task["id"])
	second_dependency = _add_task(store, "second-dependency", "Second dependency")

	with pytest.raises(InvalidDependencyError):
		store.task_dependency_add(task["id"], second_dependency["id"])


def test_inbox_add_stores_a_note_for_the_project_and_rejects_blank_text(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)

	note = store.inbox_add("Sort this later.")

	assert note["id"].startswith("inb_")
	assert note["project_id"] == PROJECT_ID
	assert note["text"] == "Sort this later."
	assert note["created_at"]
	with store.database.connection() as connection:
		assert (
			dict(
				connection.execute(
					"SELECT id, project_id, text, created_at FROM inbox_notes WHERE id = ?",
					(note["id"],),
				).fetchone()
			)
			== note
		)

	with pytest.raises(ProgressError, match="inbox note text"):
		store.inbox_add("  ")


def test_inbox_dismiss_removes_the_note_and_rejects_unknown_or_repeated_ids(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	note = store.inbox_add("Review this")
	reader = ReadStore(store.database, _ProjectStore(store.database))

	assert store.inbox_dismiss(note["id"]) == {"id": note["id"]}
	assert reader.inbox_list()["items"] == []

	for note_id in (note["id"], "inb_" + "a" * 22):
		with pytest.raises(NotFoundError) as error:
			store.inbox_dismiss(note_id)

		assert error.value.details == {"id": note_id}

	with pytest.raises(WrongObjectIdTypeError) as error:
		store.inbox_dismiss("nte_" + "n" * 22)

	assert error.value.details["expected_prefix"] == "inb_"


def test_inbox_dismiss_does_not_remove_another_projects_note(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	other_project_id = "prj_" + "q" * 22

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(other_project_id, "other", "Other project", "2026-01-01T00:00:00+00:00"),
		)

	class _OtherProjectStore:
		def current(self, path: str | Path | None = None) -> Project:
			return Project(
				other_project_id, "other", "Other project", "2026-01-01T00:00:00+00:00"
			)

	other_store = WriteStore(store.database, _OtherProjectStore())
	other_note = other_store.inbox_add("Keep this")

	with pytest.raises(NotFoundError) as error:
		store.inbox_dismiss(other_note["id"])

	assert error.value.details == {"id": other_note["id"]}
	assert (
		ReadStore(store.database, _OtherProjectStore()).inbox_list()["items"][0]["id"]
		== other_note["id"]
	)


def test_notes_and_context_replace_the_project_context_row(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	task = _add_task(store, "notes", "Notes")
	discovery = store.discovery_add(task["id"], "The schema is shared.")
	decision = store.decision_add(
		task["id"], "Keep one context row.", supersedes_id=discovery["id"]
	)
	release_note = store.discovery_add(
		None, "The release note is shared.", release_id=release["id"]
	)

	assert release_note["task_id"] is None
	assert release_note["release_id"] == release["id"]

	context = store.context_set(
		current_goal="Finish Commit 3",
		next_step="Run tests",
		verify_with="test:unit",
	)
	updated_context = store.context_set(current_goal="Finish Commit 4")

	assert discovery["id"].startswith("nte_")
	assert decision["supersedes_id"] == discovery["id"]
	assert context["next_step"] == "Run tests"
	assert context["task_id"] is None
	assert updated_context["current_goal"] == "Finish Commit 4"
	assert updated_context["next_step"] is None


def test_context_set_keeps_default_task_handoffs_independent(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	second = _add_task(store, "second", "Second")
	store.task_start(first["id"])
	store.task_start(second["id"], secondary=True)

	first_handoff = store.context_set(
		current_goal="First goal",
		previous_step="First step",
		next_step="First next",
		standing_context="First context",
		verify_with="First check",
		stop_marker="First stop",
	)
	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE projects SET default_task_id = ? WHERE id = ?",
			(second["id"], PROJECT_ID),
		)

	second_handoff = store.context_set(
		current_goal="Second goal",
		previous_step="Second step",
		next_step="Second next",
		standing_context="Second context",
		verify_with="Second check",
		stop_marker="Second stop",
	)
	updated_second = store.context_set(current_goal="Second updated")

	assert first_handoff["current_goal"] == "First goal"
	assert first_handoff["task_id"] == first["id"]
	assert second_handoff["current_goal"] == "Second goal"
	assert second_handoff["task_id"] == second["id"]
	assert updated_second["previous_step"] is None
	assert updated_second["next_step"] is None
	assert updated_second["standing_context"] is None
	assert updated_second["verify_with"] is None
	assert updated_second["stop_marker"] is None
	with store.database.connection() as connection:
		rows = connection.execute(
			"SELECT task_id, current_goal, previous_step, next_step, "
			"standing_context, verify_with, stop_marker FROM task_context ORDER BY task_id"
		).fetchall()

	assert {row["task_id"]: tuple(row)[1:] for row in rows} == {
		first["id"]: (
			"First goal",
			"First step",
			"First next",
			"First context",
			"First check",
			"First stop",
		),
		second["id"]: ("Second updated", None, None, None, None, None),
	}


def test_context_set_replaces_only_a_named_task_handoff(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	default = _add_task(store, "default", "Default")
	named = _add_task(store, "named", "Named")
	store.task_start(default["id"])
	store.context_set(current_goal="Default work", next_step="Keep going")
	first_named = store.context_set(
		task_id=named["id"],
		current_goal="Named work",
		previous_step="Started",
		next_step="Continue",
		standing_context="Keep separate",
		verify_with="Run tests",
		stop_marker="Stop here",
	)
	updated_named = store.context_set(task_id="named", current_goal="New named work")
	read_store = ReadStore(store.database, _ProjectStore(store.database))

	assert first_named["task_id"] == named["id"]
	assert updated_named["task_id"] == named["id"]
	assert updated_named["current_goal"] == "New named work"
	assert updated_named["previous_step"] is None
	assert updated_named["next_step"] is None
	assert updated_named["standing_context"] is None
	assert updated_named["verify_with"] is None
	assert updated_named["stop_marker"] is None
	assert read_store.context_get()["current_goal"] == "Default work"
	assert read_store.context_get()["next_step"] == "Keep going"

	assert read_store.context_get(task_id=named["id"])["current_goal"] == (
		"New named work"
	)


def test_context_set_refuses_a_task_from_another_project(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "other", "Other")
	other_project_id = "prj_" + "o" * 22
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(other_project_id, "other", "Other", "2026-01-01T00:00:00+00:00"),
		)
		connection.execute(
			"UPDATE tasks SET project_id = ? WHERE id = ?",
			(other_project_id, task["id"]),
		)

	with pytest.raises(NotFoundError, match="task"):
		store.context_set(task_id=task["id"], current_goal="Wrong project")


def test_task_remove_deletes_its_handoff(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	store.task_start(task["id"])
	store.context_set(current_goal="Task work")

	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_context WHERE task_id = ?", (task["id"],)
			).fetchone()
			is not None
		)

	store.task_remove(task["id"])

	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_context WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_context_set_uses_project_handoff_when_default_is_not_in_progress(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	project_handoff = store.context_set(current_goal="Project planning")
	store.task_start(task["id"])
	task_handoff = store.context_set(current_goal="Task work")

	assert project_handoff["current_goal"] == "Project planning"
	assert task_handoff["current_goal"] == "Task work"
	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE tasks SET status = 'blocked' WHERE id = ?", (task["id"],)
		)

	assert (
		ReadStore(store.database, _ProjectStore(store.database)).context_get()[
			"current_goal"
		]
		== "Project planning"
	)


def test_note_add_requires_exactly_one_owner(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	task = _add_task(store, "notes", "Notes")

	with pytest.raises(ValueError, match="exactly one"):
		store.discovery_add(task["id"], "Two owners", release_id=release["id"])
	with pytest.raises(ValueError, match="exactly one"):
		store.discovery_add(None, "No owner")


def test_remove_rejects_every_referenced_parent_row(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	task = _add_task(store, "task", "Task", release_id=release["id"])
	chunk = _add_chunk(store, task["id"], "Chunk")
	dependent = _add_task(store, "dependent", "Dependent")
	dependency = _add_task(store, "dependency", "Dependency")
	store.task_dependency_add(dependent["id"], task["id"])
	store.task_dependency_add(task["id"], dependency["id"])
	discovery = store.discovery_add(task["id"], "Discovery")
	decision = store.decision_add(task["id"], "Decision")
	release_note = store.decision_add(
		None, "Release decision", release_id=release["id"]
	)

	with pytest.raises(StillReferencedError, match=task["id"]) as release_error:
		store.release_remove(release["id"])
	assert "pass --force" in str(release_error.value)
	assert release_note["id"] in str(release_error.value)

	with pytest.raises(StillReferencedError) as error:
		store.task_remove(task["id"])

	message = str(error.value)
	assert chunk["id"] in message
	assert f"{dependent['id']} -> {task['id']}" in message
	assert f"{task['id']} -> {dependency['id']}" in message
	assert discovery["id"] in message
	assert decision["id"] in message
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

	assert "pass --force" in str(error.value)


def test_task_remove_force_deletes_owned_rows_and_unblocks_dependants(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	dependency = _add_task(store, "dependency", "Dependency")
	task = _add_task(
		store,
		"task",
		"Task",
		contract=["Task contract"],
		files=["src/task.py"],
		release_id=release["id"],
		depends_on=[dependency["id"]],
	)
	dependent = _add_task(store, "dependent", "Dependent", depends_on=[task["id"]])
	chunk = _add_chunk(store, task["id"], "Chunk")
	discovery = store.discovery_add(task["id"], "Discovery")
	decision = store.decision_add(task["id"], "Decision")
	release_note = store.discovery_add(
		None, "Release discovery", release_id=release["id"]
	)

	result = store.task_remove(task["id"], force=True)

	assert result["id"] == task["id"]
	assert set(result["deleted"]) == {
		"chunks",
		"notes",
		"dependencies",
		"tasks",
	}
	assert result["deleted"]["chunks"] == [chunk["id"]]
	assert set(result["deleted"]["notes"]) == {discovery["id"], decision["id"]}
	assert set(result["deleted"]["dependencies"]) == {
		f"{task['id']} -> {dependency['id']}",
		f"{dependent['id']} -> {task['id']}",
	}
	assert result["deleted"]["tasks"] == [task["id"]]
	assert result["unblocked_tasks"] == [dependent["id"]]

	dependent_after = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		dependent["id"]
	)
	assert dependent_after["status"] == "ready"
	assert dependent_after["status_reason"] is None

	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM tasks WHERE id = ?", (task["id"],)
			).fetchone()
			is None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM chunks WHERE id = ?", (chunk["id"],)
			).fetchone()
			is None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM notes WHERE id IN (?, ?)",
				(discovery["id"], decision["id"]),
			).fetchone()
			is None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM notes WHERE id = ?", (release_note["id"],)
			).fetchone()
			is not None
		)


def test_task_remove_keeps_a_dirty_checkout_until_it_is_clean(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)

	with pytest.raises(StillReferencedError) as error:
		store.task_remove(task["id"], path=committed_repository)

	assert str(checkout) in str(error.value)
	assert "--force" in str(error.value)
	assert checkout.is_dir()
	(checkout / "tracked.txt").write_text("committed content\n")

	result = store.task_remove(task["id"], path=committed_repository)

	assert result["removed_worktrees"] == [str(checkout)]
	assert not checkout.exists()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_remove_force_removes_an_active_dirty_checkout_but_keeps_its_branch(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	record = worktrees.ensure(task["id"], committed_repository)
	checkout = Path(record["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	with pytest.raises(StillReferencedError) as error:
		store.task_remove(task["id"], path=committed_repository)
	assert str(checkout) in str(error.value)
	assert "complete the task" in str(error.value)
	assert "--force" in str(error.value)
	assert checkout.is_dir()

	result = store.task_remove(task["id"], path=committed_repository, force=True)

	assert result["removed_worktrees"] == [str(checkout)]
	assert f"Removed checkout: {checkout}" in _render_human_output(
		"task remove", result
	)
	assert not checkout.exists()
	assert (
		GitRepository(committed_repository)
		.run(["show-ref", "--verify", "--quiet", f"refs/heads/{record['branch']}"])
		.returncode
		== 0
	)
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_remove_validates_every_id_before_removing_a_checkout(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])

	with pytest.raises(ProgressError):
		store.task_remove(
			[task["id"], "invalid"], path=committed_repository, force=True
		)

	assert checkout.is_dir()
	assert worktrees.get(task["id"], committed_repository)["path"] == str(checkout)


def test_task_remove_checks_a_later_reference_before_removing_a_checkout(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	first = _add_task(store, "first", "First", path=committed_repository)
	second = _add_task(store, "second", "Second", path=committed_repository)
	checkout = Path(worktrees.ensure(first["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(first["id"], path=committed_repository)
	(checkout / "tracked.txt").write_text("committed content\n")
	_add_chunk(store, second["id"], "Chunk", path=committed_repository)

	with pytest.raises(StillReferencedError):
		store.task_remove([first["id"], second["id"]], path=committed_repository)

	assert checkout.is_dir()
	assert worktrees.get(first["id"], committed_repository)["path"] == str(checkout)


def test_task_remove_reports_every_checkout_in_multi_id_human_output(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	tasks = [
		_add_task(store, slug, title, path=committed_repository)
		for slug, title in (("first", "First"), ("second", "Second"))
	]
	paths = [
		worktrees.ensure(task["id"], committed_repository)["path"] for task in tasks
	]

	result = store.task_remove(
		[task["id"] for task in tasks], path=committed_repository, force=True
	)
	output = _render_human_output("task remove", result)

	for checkout_path in paths:
		assert f"Removed checkout: {checkout_path}" in output


def test_task_remove_checks_a_later_in_use_checkout_before_removing_the_first(
	committed_repository: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	first = _add_task(store, "first", "First", path=committed_repository)
	second = _add_task(store, "second", "Second", path=committed_repository)
	first_checkout = Path(worktrees.ensure(first["id"], committed_repository)["path"])
	second_checkout = Path(worktrees.ensure(second["id"], committed_repository)["path"])
	monkeypatch.chdir(second_checkout)

	with pytest.raises(StillReferencedError, match="command uses the task worktree"):
		store.task_remove(
			[first["id"], second["id"]], path=committed_repository, force=True
		)

	assert first_checkout.is_dir()
	assert worktrees.get(first["id"], committed_repository)["path"] == str(
		first_checkout
	)


def test_task_remove_keeps_a_locked_checkout_even_with_force(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	assert (
		GitRepository(committed_repository)
		.run(["worktree", "lock", str(checkout)])
		.returncode
		== 0
	)

	with pytest.raises(StillReferencedError, match="worktree is locked"):
		store.task_remove(task["id"], path=committed_repository, force=True)

	assert checkout.is_dir()
	assert worktrees.get(task["id"], committed_repository)["path"] == str(checkout)


def test_task_remove_reports_a_folder_left_after_a_later_git_failure(
	committed_repository: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	first = _add_task(store, "first", "First", path=committed_repository)
	second = _add_task(store, "second", "Second", path=committed_repository)
	first_checkout = Path(worktrees.ensure(first["id"], committed_repository)["path"])
	second_checkout = Path(worktrees.ensure(second["id"], committed_repository)["path"])
	original_run = GitRepository.run

	def fail_second_removal(self, arguments):
		"""Leave the second checkout on disk after the first is removed."""
		if arguments == ["worktree", "remove", "--force", str(second_checkout)]:
			return subprocess.CompletedProcess(arguments, 1, "", "Git refused removal")
		return original_run(self, arguments)

	with monkeypatch.context() as patch:
		patch.setattr(GitRepository, "run", fail_second_removal)
		result = store.task_remove(
			[first["id"], second["id"]], path=committed_repository, force=True
		)

	assert result[0]["removed_worktrees"] == [str(first_checkout)]
	assert result[1]["left_worktrees"] == [
		{"path": str(second_checkout), "reason": "Git refused removal"}
	]
	assert (
		f"Left checkout on disk: {second_checkout} (Git refused removal). "
		"Remove it by hand or with git worktree remove."
	) in _render_human_output("task remove", result)
	assert not first_checkout.exists()
	assert second_checkout.is_dir()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM tasks WHERE id IN (?, ?)", (first["id"], second["id"])
			).fetchone()
			is None
		)


def test_task_remove_checks_a_later_note_refusal_before_removing_a_checkout(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	first = _add_task(store, "first", "First", path=committed_repository)
	second = _add_task(store, "second", "Second", path=committed_repository)
	other = _add_task(store, "other", "Other", path=committed_repository)
	checkout = Path(worktrees.ensure(first["id"], committed_repository)["path"])
	note = store.discovery_add(second["id"], "Second note", path=committed_repository)
	store.decision_add(
		other["id"],
		"Outside note",
		supersedes_id=note["id"],
		path=committed_repository,
	)

	with pytest.raises(StillReferencedError, match="remove the superseding note first"):
		store.task_remove(
			[first["id"], second["id"]], path=committed_repository, force=True
		)

	assert checkout.is_dir()
	assert worktrees.get(first["id"], committed_repository)["path"] == str(checkout)


@pytest.mark.parametrize("remove_release", [False, True])
def test_force_remove_leaves_a_folder_with_an_unsafe_recorded_path(
	committed_repository: Path,
	remove_release: bool,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	release = (
		store.release_add(
			"release", "Release", overview="Release overview", path=committed_repository
		)
		if remove_release
		else None
	)
	task = _add_task(
		store,
		"task",
		"Task",
		path=committed_repository,
		release_id=release["id"] if release else None,
	)
	managed = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	foreign = committed_repository.parent / "foreign-checkout"
	foreign.mkdir()
	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE task_worktrees SET path = ? WHERE task_id = ?",
			(str(foreign), task["id"]),
		)

	if remove_release:
		result = store.release_remove(
			release["id"], path=committed_repository, force=True
		)
		output = _render_human_output("release remove", result)
	else:
		result = store.task_remove(task["id"], path=committed_repository, force=True)
		output = _render_human_output("task remove", result)

	assert result["left_worktrees"] == [
		{"path": str(foreign), "reason": "outside the managed location"}
	]
	assert (
		f"Left checkout on disk: {foreign} (outside the managed location). "
		"Remove it by hand or with git worktree remove."
	) in output
	assert foreign.is_dir()
	assert managed.is_dir()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


@pytest.mark.parametrize("validation_failure", ["record", "checkout"])
def test_force_remove_leaves_a_folder_when_checkout_validation_fails(
	committed_repository: Path,
	validation_failure: str,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)
	if validation_failure == "record":
		with store.database.transaction() as connection:
			connection.execute(
				"UPDATE task_worktrees SET common_dir = ? WHERE task_id = ?",
				(str(committed_repository / "other-git-dir"), task["id"]),
			)
	else:
		assert (
			GitRepository(checkout).run(["switch", "-c", "different-branch"]).returncode
			== 0
		)

	with pytest.raises(StillReferencedError, match=str(checkout)):
		store.task_remove(task["id"], path=committed_repository)

	result = store.task_remove(task["id"], path=committed_repository, force=True)

	assert result["left_worktrees"][0]["path"] == str(checkout)
	assert result["left_worktrees"][0]["reason"]
	assert f"Left checkout on disk: {checkout}" in _render_human_output(
		"task remove", result
	)
	assert checkout.is_dir()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_release_remove_force_removes_a_dirty_checkout_but_keeps_its_branch(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	release = store.release_add(
		"release", "Release", overview="Release overview", path=committed_repository
	)
	task = _add_task(
		store, "task", "Task", release_id=release["id"], path=committed_repository
	)
	record = worktrees.ensure(task["id"], committed_repository)
	checkout = Path(record["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")

	result = store.release_remove(release["id"], path=committed_repository, force=True)

	assert result["removed_worktrees"] == [str(checkout)]
	assert f"Removed checkout: {checkout}" in _render_human_output(
		"release remove", result
	)
	assert not checkout.exists()
	assert (
		GitRepository(committed_repository)
		.run(["show-ref", "--verify", "--quiet", f"refs/heads/{record['branch']}"])
		.returncode
		== 0
	)
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_release_remove_validates_every_id_before_removing_a_checkout(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	release = store.release_add(
		"release", "Release", overview="Release overview", path=committed_repository
	)
	task = _add_task(
		store, "task", "Task", release_id=release["id"], path=committed_repository
	)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])

	with pytest.raises(ProgressError):
		store.release_remove(
			[release["id"], "invalid"], path=committed_repository, force=True
		)

	assert checkout.is_dir()
	assert worktrees.get(task["id"], committed_repository)["path"] == str(checkout)


def test_release_remove_checks_a_later_reference_before_removing_a_checkout(
	committed_repository: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	first = store.release_add(
		"first", "First", overview="First overview", path=committed_repository
	)
	second = store.release_add(
		"second", "Second", overview="Second overview", path=committed_repository
	)
	task = _add_task(
		store, "task", "Task", release_id=first["id"], path=committed_repository
	)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	second_task = _add_task(
		store,
		"second-task",
		"Second task",
		release_id=second["id"],
		path=committed_repository,
	)
	second_checkout = Path(
		worktrees.ensure(second_task["id"], committed_repository)["path"]
	)
	monkeypatch.chdir(second_checkout)

	with pytest.raises(StillReferencedError):
		store.release_remove(
			[first["id"], second["id"]], path=committed_repository, force=True
		)

	assert checkout.is_dir()
	assert worktrees.get(task["id"], committed_repository)["path"] == str(checkout)


def test_release_remove_force_deletes_tasks_and_release_rows(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	first_task = _add_task(store, "first-task", "First task", release_id=release["id"])
	second_task = _add_task(
		store,
		"second-task",
		"Second task",
		release_id=release["id"],
		depends_on=[first_task["id"]],
	)
	chunk = _add_chunk(store, first_task["id"], "Chunk")
	task_note = store.discovery_add(first_task["id"], "Task note")
	release_note = store.decision_add(None, "Release note", release_id=release["id"])

	result = store.release_remove(release["id"], force=True)

	assert result["id"] == release["id"]
	assert set(result["deleted"]["tasks"]) == {
		first_task["id"],
		second_task["id"],
	}
	assert result["deleted"]["chunks"] == [chunk["id"]]
	assert set(result["deleted"]["notes"]) == {
		task_note["id"],
		release_note["id"],
	}
	assert result["deleted"]["dependencies"] == [
		f"{second_task['id']} -> {first_task['id']}"
	]
	assert result["unblocked_tasks"] == []

	with store.database.connection() as connection:
		for table in ("releases", "tasks", "chunks", "notes"):
			assert (
				connection.execute(
					f"SELECT 1 FROM {table} WHERE id IN (?, ?, ?, ?)",
					(
						release["id"],
						first_task["id"],
						second_task["id"],
						chunk["id"],
					),
				).fetchone()
				is None
			)
		for note_id in (task_note["id"], release_note["id"]):
			assert (
				connection.execute(
					"SELECT 1 FROM notes WHERE id = ?", (note_id,)
				).fetchone()
				is None
			)


def test_release_remove_force_does_not_report_deleted_dependants_as_unblocked(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	first_task = _add_task(store, "first-task", "First task", release_id=release["id"])
	second_task = _add_task(
		store, "second-task", "Second task", release_id=release["id"]
	)
	ordered_task_ids = sorted((first_task["id"], second_task["id"]))
	store.task_dependency_add(ordered_task_ids[1], ordered_task_ids[0])

	result = store.release_remove(release["id"], force=True)

	assert ordered_task_ids[1] in result["deleted"]["tasks"]
	assert ordered_task_ids[1] not in result["unblocked_tasks"]


def test_force_remove_rolls_back_when_a_later_id_fails(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	chunk = _add_chunk(store, task["id"], "Chunk")
	failing_id = "tsk_" + "m" * 22

	with pytest.raises(NotFoundError, match=failing_id):
		store.task_remove([task["id"], failing_id], force=True)

	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM chunks WHERE id = ?", (chunk["id"],)
			).fetchone()
			is not None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM tasks WHERE id = ?", (task["id"],)
			).fetchone()
			is not None
		)


def test_task_remove_force_refuses_notes_superseded_outside_the_cascade(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	other_task = _add_task(store, "other-task", "Other task")
	note = store.discovery_add(task["id"], "Task note")
	outside_note = store.decision_add(
		other_task["id"],
		"Outside note",
		supersedes_id=note["id"],
	)

	with pytest.raises(StillReferencedError) as error:
		store.task_remove(task["id"], force=True)

	assert "remove the superseding note first" in str(error.value)
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM tasks WHERE id = ?", (task["id"],)
			).fetchone()
			is not None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM notes WHERE id IN (?, ?)",
				(note["id"], outside_note["id"]),
			).fetchone()
			is not None
		)


def test_remove_note_rejects_a_superseded_note(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
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
	release = store.release_add("release", "Release", overview="Release overview")
	task = _add_task(store, "task", "Task", release_id=release["id"])
	dependency = _add_task(store, "dependency", "Dependency")
	chunk = _add_chunk(store, task["id"], "Chunk")
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


@pytest.mark.parametrize(
	("method_name", "record_type"),
	[
		("release_remove", "release"),
		("release_complete", "release"),
		("task_remove", "task"),
		("task_complete", "task"),
		("chunk_remove", "chunk"),
		("chunk_complete", "chunk"),
	],
)
def test_write_methods_return_multiple_results_in_input_order(
	tmp_path: Path, method_name: str, record_type: str
) -> None:
	store = _seed_store(tmp_path)

	if record_type == "release":
		records = [
			store.release_add("first-release", "First release", overview="Overview"),
			store.release_add("second-release", "Second release", overview="Overview"),
		]
	elif record_type == "task":
		records = [
			_add_task(store, "first-task", "First task"),
			_add_task(store, "second-task", "Second task"),
		]
	else:
		task = _add_task(store, "chunk-task", "Chunk task")
		records = [
			_add_chunk(store, task["id"], "First chunk"),
			_add_chunk(store, task["id"], "Second chunk"),
		]

	identifiers = [record["id"] for record in reversed(records)]
	results = getattr(store, method_name)(identifiers)

	assert [result["id"] for result in results] == identifiers


@pytest.mark.parametrize(
	("method_name", "record_type"),
	[
		("release_remove", "release"),
		("release_complete", "release"),
		("task_remove", "task"),
		("task_complete", "task"),
		("chunk_remove", "chunk"),
		("chunk_complete", "chunk"),
	],
)
def test_write_methods_roll_back_earlier_ids_when_a_later_id_fails(
	tmp_path: Path, method_name: str, record_type: str
) -> None:
	store = _seed_store(tmp_path)

	if record_type == "release":
		record = store.release_add("release", "Release", overview="Overview")
		if method_name == "release_complete":
			failing_record = store.release_add(
				"done-release", "Done release", overview="Overview", status="done"
			)
		else:
			failing_id = "rel_" + "m" * 22
			failing_record = {"id": failing_id}
	elif record_type == "task":
		record = _add_task(store, "task", "Task")
		if method_name == "task_complete":
			failing_record = _add_task(store, "done-task", "Done task")
			store.task_complete(failing_record["id"])
		else:
			failing_id = "tsk_" + "m" * 22
			failing_record = {"id": failing_id}
	else:
		task = _add_task(store, "chunk-task", "Chunk task")
		record = _add_chunk(store, task["id"], "Chunk")
		if method_name == "chunk_complete":
			failing_record = _add_chunk(store, task["id"], "Done chunk")
			store.chunk_complete(failing_record["id"])
		else:
			failing_id = "chk_" + "m" * 22
			failing_record = {"id": failing_id}

	with pytest.raises(ProgressError, match=failing_record["id"]):
		getattr(store, method_name)([record["id"], failing_record["id"]])

	if record_type == "release":
		remaining = ReadStore(
			store.database, _ProjectStore(store.database)
		).release_get(record["id"])
		assert remaining["status"] == "planned"
	elif record_type == "task":
		remaining = ReadStore(store.database, _ProjectStore(store.database)).task_get(
			record["id"]
		)
		assert remaining["status"] == "ready"
	else:
		remaining = ReadStore(store.database, _ProjectStore(store.database)).chunk_get(
			record["id"]
		)
		assert remaining["status"] == "pending"


def test_task_clean_removes_safe_done_tasks_and_reports_blockers(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	empty_release = store.release_add(
		"empty", "Empty release", overview="Empty release overview"
	)
	clean_release = store.release_add(
		"clean", "Clean release", overview="Clean release overview"
	)
	mixed_release = store.release_add(
		"mixed", "Mixed release", overview="Mixed release overview"
	)
	blocked_release = store.release_add(
		"blocked", "Blocked release", overview="Blocked release overview"
	)

	clean_task = _add_task(store, "clean", "Clean task", release_id=clean_release["id"])
	clean_chunk = _add_chunk(store, clean_task["id"], "Clean chunk")
	store.task_start(clean_task["id"])
	store.chunk_complete(clean_chunk["id"])
	store.task_complete(clean_task["id"])

	mixed_task = _add_task(store, "mixed", "Mixed task", release_id=mixed_release["id"])
	store.task_start(mixed_task["id"])
	store.task_complete(mixed_task["id"])
	_add_task(store, "remaining", "Remaining task", release_id=mixed_release["id"])

	dependency = _add_task(
		store, "dependency", "Dependency", release_id=blocked_release["id"]
	)
	blocked_task = _add_task(
		store,
		"blocked",
		"Blocked task",
		release_id=blocked_release["id"],
		depends_on=[dependency["id"]],
	)
	discovery = store.discovery_add(blocked_task["id"], "Keep this discovery.")
	store.decision_add(
		blocked_task["id"],
		"Keep this decision.",
		supersedes_id=discovery["id"],
	)
	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	store.task_start(blocked_task["id"])
	store.task_complete(blocked_task["id"])

	result = store.task_clean()
	blocked_by_id = {task["id"]: task for task in result["blocked"]}

	assert result["removed_count"] == 2
	assert {task["id"] for task in result["removed"]} == {
		clean_task["id"],
		mixed_task["id"],
	}
	assert blocked_by_id[blocked_task["id"]]["notes"] == [
		{"type": "discovery", "body": "Keep this discovery."},
		{"type": "decision", "body": "Keep this decision."},
	]
	assert blocked_by_id[blocked_task["id"]]["dependencies"] == [
		{
			"task_id": blocked_task["id"],
			"depends_on_task_id": dependency["id"],
			"other_task_id": dependency["id"],
			"other_task_title": "Dependency",
			"direction": "depends_on",
		}
	]
	assert blocked_by_id[dependency["id"]]["dependencies"] == [
		{
			"task_id": blocked_task["id"],
			"depends_on_task_id": dependency["id"],
			"other_task_id": blocked_task["id"],
			"other_task_title": "Blocked task",
			"direction": "required_by",
		}
	]
	assert result["releases_removed"] == [
		{"id": clean_release["id"], "title": "Clean release"}
	]

	read_store = ReadStore(store.database, _ProjectStore(store.database))
	with pytest.raises(NotFoundError):
		read_store.task_get(clean_task["id"])
	assert {task["id"] for task in read_store.task_list(status="done")["items"]} == {
		dependency["id"],
		blocked_task["id"],
	}
	release_ids = {release["id"] for release in read_store.release_list()["items"]}
	assert clean_release["id"] not in release_ids
	assert empty_release["id"] in release_ids
	assert mixed_release["id"] in release_ids
	assert blocked_release["id"] in release_ids


def test_task_clean_deletes_contract_and_file_rows(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(
		store,
		"task",
		"Task",
		contract=["Task step"],
		files=["src/task.py"],
	)
	chunk = _add_chunk(store, task["id"], "Chunk")
	store.task_start(task["id"])
	store.chunk_complete(chunk["id"])
	store.task_complete(task["id"])

	result = store.task_clean()

	assert result["removed"] == [{"id": task["id"], "title": "Task"}]
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_contract_steps WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM task_files WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_clean_keeps_a_done_task_with_a_dirty_checkout(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)

	result = store.task_clean(path=committed_repository)

	assert result["removed"] == []
	assert result["blocked"][0]["id"] == task["id"]
	assert result["blocked"][0]["worktree"]["path"] == str(checkout)
	assert "--force" in result["blocked"][0]["worktree"]["reason"]
	assert f"Kept checkout  {checkout}" in _render_human_output("task clean", result)
	assert checkout.is_dir()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is not None
		)


def test_task_clean_renders_the_whole_output_for_a_checkout_only_blocker() -> None:
	data = {
		"removed_count": 0,
		"removed": [],
		"blocked": [
			{
				"id": "tsk_example",
				"title": "Task",
				"notes": [],
				"dependencies": [],
				"worktree": {
					"path": "/tmp/task-checkout",
					"reason": "command uses the task worktree",
				},
			}
		],
		"releases_removed": [],
	}

	output = _render_human_output("task clean", data)

	assert (
		output
		== "\n".join(
			[
				render_span("0 tasks removed", "success", weight="normal"),
				render_span("1 task kept", "warning", weight="normal"),
				"",
				f"{render_span('Kept task'.ljust(len('Kept checkout')), 'muted', weight='normal')}  Task (tsk_example)",
				f"{render_span('Kept checkout', 'muted', weight='normal')}  /tmp/task-checkout (command uses the task worktree)",
				"",
				render_span("0 releases removed", "muted", weight="normal"),
			]
		)
		+ "\n"
	)


def test_task_clean_skips_a_task_that_becomes_done_after_checkout_inspection(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	second = _add_task(store, "second", "Second")
	store.task_complete(first["id"])
	original_inspect = WorktreeStore.inspect_cleanup

	def mark_second_done(self, task_id, path=None, *, force=False):
		"""Finish the second task after clean has selected its inspection IDs."""
		inspection = original_inspect(self, task_id, path, force=force)
		with store.database.transaction() as connection:
			connection.execute(
				"UPDATE tasks SET status = 'done' WHERE id = ?", (second["id"],)
			)
		return inspection

	with monkeypatch.context() as patch:
		patch.setattr(WorktreeStore, "inspect_cleanup", mark_second_done)
		first_pass = store.task_clean()

	assert first_pass["removed"] == [{"id": first["id"], "title": "First"}]
	assert (
		ReadStore(store.database, _ProjectStore(store.database)).task_get(second["id"])[
			"status"
		]
		== "done"
	)

	second_pass = store.task_clean()

	assert second_pass["removed"] == [{"id": second["id"], "title": "Second"}]


def test_task_clean_removes_a_checkout_after_its_changes_are_discarded(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)
	(checkout / "tracked.txt").write_text("committed content\n")

	result = store.task_clean(path=committed_repository)

	assert result["removed"] == [{"id": task["id"], "title": "Task"}]
	assert result["removed_worktrees"] == [str(checkout)]
	assert f"Removed checkout: {checkout}" in _render_human_output("task clean", result)
	assert not checkout.exists()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_clean_force_removes_a_dirty_checkout_but_keeps_its_branch(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	record = worktrees.ensure(task["id"], committed_repository)
	checkout = Path(record["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)

	result = store.task_clean(force=True, path=committed_repository)

	assert result["removed_worktrees"] == [str(checkout)]
	assert not checkout.exists()
	assert (
		GitRepository(committed_repository)
		.run(["show-ref", "--verify", "--quiet", f"refs/heads/{record['branch']}"])
		.returncode
		== 0
	)
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_clean_force_keeps_an_in_use_checkout_and_removes_other_done_tasks(
	committed_repository: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	kept = _add_task(store, "kept", "Kept", path=committed_repository)
	removed = _add_task(store, "removed", "Removed", path=committed_repository)
	kept_path = Path(worktrees.ensure(kept["id"], committed_repository)["path"])
	removed_path = Path(worktrees.ensure(removed["id"], committed_repository)["path"])
	(kept_path / "tracked.txt").write_text("unfinished work\n")
	(removed_path / "tracked.txt").write_text("unfinished work\n")
	store.task_complete([kept["id"], removed["id"]], path=committed_repository)
	monkeypatch.chdir(kept_path)

	result = store.task_clean(force=True, path=committed_repository)
	output = _render_human_output("task clean", result)

	assert result["removed"] == [{"id": removed["id"], "title": "Removed"}]
	assert result["blocked"][0]["worktree"] == {
		"path": str(kept_path),
		"reason": "command uses the task worktree",
	}
	assert f"Kept checkout  {kept_path} (command uses the task worktree)" in output
	assert f"Removed checkout: {removed_path}" in output
	assert kept_path.is_dir()
	assert not removed_path.exists()


def test_task_clean_never_removes_a_recorded_folder_outside_the_managed_location(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	managed = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(managed / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)
	foreign = committed_repository.parent / "foreign-checkout"
	foreign.mkdir()
	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE task_worktrees SET path = ? WHERE task_id = ?",
			(str(foreign), task["id"]),
		)

	kept = store.task_clean(path=committed_repository)
	removed = store.task_clean(force=True, path=committed_repository)

	assert kept["blocked"][0]["worktree"]["path"] == str(foreign)
	assert removed["left_worktrees"] == [
		{"path": str(foreign), "reason": "outside the managed location"}
	]
	assert f"Left checkout on disk: {foreign}" in _render_human_output(
		"task clean", removed
	)
	assert foreign.is_dir()
	assert managed.is_dir()


@pytest.mark.parametrize("validation_failure", ["record", "checkout"])
def test_task_clean_force_clears_an_invalid_checkout_record_without_removing_its_folder(
	committed_repository: Path,
	validation_failure: str,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)
	if validation_failure == "record":
		with store.database.transaction() as connection:
			connection.execute(
				"UPDATE task_worktrees SET common_dir = ? WHERE task_id = ?",
				(str(committed_repository / "other-git-dir"), task["id"]),
			)
	else:
		assert (
			GitRepository(checkout).run(["switch", "-c", "different-branch"]).returncode
			== 0
		)

	kept = store.task_clean(path=committed_repository)
	removed = store.task_clean(force=True, path=committed_repository)

	assert kept["blocked"][0]["worktree"]["path"] == str(checkout)
	assert removed["removed"] == [{"id": task["id"], "title": "Task"}]
	assert removed["left_worktrees"][0]["path"] == str(checkout)
	assert removed["left_worktrees"][0]["reason"]
	assert f"Left checkout on disk: {checkout}" in _render_human_output(
		"task clean", removed
	)
	assert checkout.is_dir()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_clean_force_clears_an_invalid_record_for_a_missing_checkout(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)
	with store.database.transaction() as connection:
		connection.execute(
			"UPDATE task_worktrees SET common_dir = ? WHERE task_id = ?",
			(str(committed_repository / "other-git-dir"), task["id"]),
		)
	assert (
		GitRepository(committed_repository)
		.run(["worktree", "remove", "--force", str(checkout)])
		.returncode
		== 0
	)

	result = store.task_clean(force=True, path=committed_repository)

	assert result["removed"] == [{"id": task["id"], "title": "Task"}]
	assert result.get("left_worktrees", []) == []
	assert "Left checkout on disk:" not in _render_human_output("task clean", result)
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_clean_clears_a_record_for_an_already_absent_checkout(
	committed_repository: Path,
) -> None:
	store, worktrees = _git_backed_completion_store(committed_repository)
	task = _add_task(store, "task", "Task", path=committed_repository)
	checkout = Path(worktrees.ensure(task["id"], committed_repository)["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")
	store.task_complete(task["id"], path=committed_repository)
	assert (
		GitRepository(committed_repository)
		.run(["worktree", "remove", "--force", str(checkout)])
		.returncode
		== 0
	)

	result = store.task_clean(path=committed_repository)

	assert result["removed"] == [{"id": task["id"], "title": "Task"}]
	assert result.get("removed_worktrees", []) == []
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_worktrees WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_clean_force_removes_done_tasks_and_notes(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("blocked", "Blocked", overview="Blocked overview")
	dependency = _add_task(store, "dependency", "Dependency", release_id=release["id"])
	blocked_task = _add_task(
		store,
		"blocked",
		"Blocked task",
		release_id=release["id"],
		depends_on=[dependency["id"]],
	)
	discovery = store.discovery_add(blocked_task["id"], "Discovery body")
	store.decision_add(
		blocked_task["id"], "Decision body", supersedes_id=discovery["id"]
	)
	release_note = store.discovery_add(
		None, "Release discovery", release_id=release["id"]
	)
	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	store.task_start(blocked_task["id"])
	store.task_complete(blocked_task["id"])

	store.task_clean()
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM notes WHERE id = ?", (release_note["id"],)
			).fetchone()
			is not None
		)
	new_clean_task = _add_task(store, "new-clean", "New clean task")
	store.task_start(new_clean_task["id"])
	store.task_complete(new_clean_task["id"])

	result = store.task_clean(force=True)

	assert result["removed_count"] == 3
	assert {task["id"] for task in result["removed"]} == {
		dependency["id"],
		blocked_task["id"],
		new_clean_task["id"],
	}
	assert result["blocked"] == []
	assert result["releases_removed"] == [{"id": release["id"], "title": "Blocked"}]
	with pytest.raises(NotFoundError):
		ReadStore(store.database, _ProjectStore(store.database)).task_get(
			new_clean_task["id"]
		)

	with store.database.connection() as connection:
		assert connection.execute("SELECT 1 FROM notes").fetchone() is None
		assert connection.execute("SELECT 1 FROM task_dependencies").fetchone() is None


def test_task_clean_force_removes_safe_and_blocked_tasks_in_one_pass(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	safe_release = store.release_add("safe", "Safe release", overview="Safe overview")
	blocked_release = store.release_add(
		"blocked", "Blocked release", overview="Blocked overview"
	)
	safe_task = _add_task(store, "safe", "Safe task", release_id=safe_release["id"])
	dependency = _add_task(
		store, "dependency", "Dependency", release_id=blocked_release["id"]
	)
	blocked_task = _add_task(
		store,
		"blocked",
		"Blocked task",
		release_id=blocked_release["id"],
		depends_on=[dependency["id"]],
	)
	safe_chunk = _add_chunk(store, safe_task["id"], "Safe chunk")
	blocked_chunk = _add_chunk(store, blocked_task["id"], "Blocked chunk")
	store.discovery_add(blocked_task["id"], "Blocked discovery")
	store.task_start(safe_task["id"])
	store.chunk_complete(safe_chunk["id"])
	store.task_complete(safe_task["id"])
	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	store.task_start(blocked_task["id"])
	store.chunk_complete(blocked_chunk["id"])
	store.task_complete(blocked_task["id"])

	result = store.task_clean(force=True)

	assert result["removed_count"] == 3
	assert {task["id"] for task in result["removed"]} == {
		safe_task["id"],
		dependency["id"],
		blocked_task["id"],
	}
	assert result["blocked"] == []
	assert {release["id"] for release in result["releases_removed"]} == {
		safe_release["id"],
		blocked_release["id"],
	}
	with store.database.connection() as connection:
		for table in ("tasks", "notes", "task_dependencies", "chunks", "releases"):
			assert connection.execute(f"SELECT 1 FROM {table}").fetchone() is None


def test_rename_updates_titles_without_changing_identifiers_or_slugs(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	task = _add_task(store, "task", "Task", release_id=release["id"])
	chunk = _add_chunk(store, task["id"], "Chunk")

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
	release = store.release_add("release", "Release", overview="Release overview")
	task = _add_task(
		store,
		"task",
		"Task",
		overview="Original overview",
		contract=["Original contract"],
		files=["original.py"],
		verification="Original verification",
		release_id=release["id"],
		position=3,
	)

	updated = store.task_edit(
		task["id"],
		overview="Updated overview",
		files=["updated.py"],
	)

	assert updated["id"] == task["id"]
	assert updated["project_id"] == task["project_id"]
	assert updated["slug"] == task["slug"]
	assert updated["release_id"] == task["release_id"]
	assert updated["title"] == task["title"]
	assert updated["overview"] == "Updated overview"
	assert updated["contract"] == task["contract"]
	assert updated["files"] == ["updated.py"]
	assert updated["verification"] == task["verification"]
	assert updated["status"] == task["status"]
	assert updated["status_reason"] == task["status_reason"]
	assert updated["position"] == task["position"]
	assert updated["created_at"] == task["created_at"]
	assert updated["started_at"] == task["started_at"]
	assert updated["completed_at"] == task["completed_at"]
	assert updated["updated_at"] == task["updated_at"]


def test_task_edit_clears_task_files(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task", files=["src/task.py"])

	updated = store.task_edit(task["id"], clear_files=True)

	assert updated["files"] == []
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_files WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_edit_updates_and_clears_split_rationale(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task", split_rationale="Initial rationale")
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT split_rationale FROM tasks WHERE id = ?", (task["id"],)
			).fetchone()[0]
			== "Initial rationale"
		)

	updated = store.task_edit(task["id"], split_rationale="Updated rationale")

	assert updated["id"] == task["id"]
	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT split_rationale FROM tasks WHERE id = ?", (task["id"],)
			).fetchone()[0]
			== "Updated rationale"
		)

	store.task_edit(task["id"], clear_split_rationale=True)

	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT split_rationale FROM tasks WHERE id = ?", (task["id"],)
			).fetchone()[0]
			is None
		)


def test_task_contract_and_files_round_trip_in_order(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(
		store,
		"task",
		"Task",
		contract=["First step", "Second step"],
		files=["src/first.py", "src/second.py"],
	)

	assert task["contract"] == ["First step", "Second step"]
	assert task["files"] == ["src/first.py", "src/second.py"]
	with store.database.connection() as connection:
		assert [
			tuple(row)
			for row in connection.execute(
				"SELECT position, text FROM task_contract_steps WHERE task_id = ? ORDER BY position",
				(task["id"],),
			).fetchall()
		] == [(1, "First step"), (2, "Second step")]
		assert [
			tuple(row)
			for row in connection.execute(
				"SELECT position, text FROM task_files WHERE task_id = ? ORDER BY position",
				(task["id"],),
			).fetchall()
		] == [(1, "src/first.py"), (2, "src/second.py")]

	updated = store.task_edit(
		task["id"],
		contract=("Updated first", "Updated second", "Updated third"),
		files=("src/updated.py",),
	)

	assert updated["contract"] == [
		"Updated first",
		"Updated second",
		"Updated third",
	]
	assert updated["files"] == ["src/updated.py"]


def test_task_remove_deletes_contract_and_file_rows(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(
		store,
		"task",
		"Task",
		contract=["Task step"],
		files=["src/task.py"],
	)

	store.task_remove(task["id"])

	with store.database.connection() as connection:
		assert (
			connection.execute(
				"SELECT 1 FROM task_contract_steps WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)
		assert (
			connection.execute(
				"SELECT 1 FROM task_files WHERE task_id = ?", (task["id"],)
			).fetchone()
			is None
		)


def test_task_edit_validates_all_values_before_writing(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")

	with pytest.raises(ProgressError, match="must be text"):
		store.task_edit(task["id"], overview=object())  # type: ignore[arg-type]

	current = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		task["id"]
	)

	assert {key: current[key] for key in task} == task
	assert current["chunks"] == []


def test_task_edit_requires_at_least_one_field(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")

	with pytest.raises(ProgressError, match="requires at least one field"):
		store.task_edit(task["id"])


@pytest.mark.parametrize(
	("field", "value"),
	[
		("overview", ""),
		("contract", [""]),
	],
)
def test_task_edit_rejects_blank_required_text(
	tmp_path: Path, field: str, value: str | list[str]
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")

	with pytest.raises(ProgressError, match=f"task {field}"):
		store.task_edit(task["id"], **{field: value})


def test_chunk_edit_updates_description_and_preserves_lifecycle_data(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	chunk = _add_chunk(store, task["id"], "Chunk", description="Original description")

	updated = store.chunk_edit(chunk["id"], description="Updated description")

	assert updated == {**chunk, "description": "Updated description"}


def test_chunk_edit_allows_an_unchanged_description(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	chunk = _add_chunk(store, task["id"], "Chunk", description="Original description")

	updated = store.chunk_edit(chunk["id"], description="Original description")

	assert updated == chunk


def test_chunk_edit_requires_a_description_input(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	chunk = _add_chunk(store, task["id"], "Chunk")

	with pytest.raises(ProgressError, match="requires"):
		store.chunk_edit(chunk["id"])


@pytest.mark.parametrize("description", ["", " \t"])
def test_chunk_edit_rejects_blank_description(tmp_path: Path, description: str) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	chunk = _add_chunk(store, task["id"], "Chunk")

	with pytest.raises(ProgressError, match="chunk description"):
		store.chunk_edit(chunk["id"], description=description)


def test_chunk_edit_updates_only_the_review_question(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	chunk = _add_chunk(store, task["id"], "Chunk", description="Original description")

	updated = store.chunk_edit(
		chunk["id"], review_question="Is the retry policy right?"
	)

	assert updated == {**chunk, "review_question": "Is the retry policy right?"}


@pytest.mark.parametrize("review_question", ["", " \t"])
def test_chunk_edit_rejects_a_blank_review_question(
	tmp_path: Path, review_question: str
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")
	chunk = _add_chunk(store, task["id"], "Chunk")

	with pytest.raises(ProgressError, match="chunk review question"):
		store.chunk_edit(chunk["id"], review_question=review_question)


@pytest.mark.parametrize("review_question", ["", " \t"])
def test_chunk_add_rejects_a_blank_review_question(
	tmp_path: Path, review_question: str
) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")

	with pytest.raises(ProgressError, match="chunk review question"):
		_add_chunk(store, task["id"], "Chunk", review_question=review_question)

	chunks = ReadStore(store.database, _ProjectStore(store.database)).chunk_list(
		task["id"]
	)

	assert chunks["items"] == []


def test_release_move_reorders_releases_and_normalises_positions(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	first = store.release_add("first", "First", overview="First overview")
	second = store.release_add("second", "Second", overview="Second overview")
	third = store.release_add("third", "Third", overview="Third overview")

	moved = store.release_move(third["id"], before_release_id=first["id"])
	releases = ReadStore(store.database, _ProjectStore(store.database)).release_list()

	assert moved["id"] == third["id"]
	assert moved["position"] == 1
	assert [release["id"] for release in releases["items"]] == [
		third["id"],
		first["id"],
		second["id"],
	]
	assert [release["position"] for release in releases["items"]] == [1, 2, 3]

	store.release_move(first["id"], after_release_id=second["id"])
	releases = ReadStore(store.database, _ProjectStore(store.database)).release_list()

	assert [release["id"] for release in releases["items"]] == [
		third["id"],
		second["id"],
		first["id"],
	]


def test_release_move_can_move_a_release_to_the_end(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = store.release_add("first", "First", overview="First overview")
	second = store.release_add("second", "Second", overview="Second overview")
	third = store.release_add("third", "Third", overview="Third overview")

	moved = store.release_move(first["id"], after_release_id=third["id"])
	releases = ReadStore(store.database, _ProjectStore(store.database)).release_list()

	assert moved["position"] == 3
	assert [release["id"] for release in releases["items"]] == [
		second["id"],
		third["id"],
		first["id"],
	]


def test_release_move_rejects_a_self_relative_target(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")

	with pytest.raises(InvalidTransitionError, match="cannot move relative to itself"):
		store.release_move(release["id"], before_release_id=release["id"])


def test_release_move_rejects_unknown_moved_and_target_releases(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	unknown_release_id = "rel_" + "u" * 22

	with pytest.raises(
		NotFoundError, match=f"release {unknown_release_id} was not found"
	):
		store.release_move(unknown_release_id, before_release_id=release["id"])

	with pytest.raises(
		NotFoundError, match=f"release {unknown_release_id} was not found"
	):
		store.release_move(release["id"], before_release_id=unknown_release_id)


def test_release_move_is_scoped_to_the_current_project(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	other_project_id = "prj_" + "o" * 22
	other_release_id = "rel_" + "o" * 22

	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(other_project_id, "other", "Other project", "2026-01-01T00:00:00+00:00"),
		)
		connection.execute(
			"""
			INSERT INTO releases (id, project_id, slug, title, overview, status, position)
			VALUES (?, ?, ?, ?, ?, ?, ?)
			""",
			(
				other_release_id,
				other_project_id,
				"other-release",
				"Other release",
				"Other release overview",
				"planned",
				1,
			),
		)

	with pytest.raises(
		NotFoundError, match=f"release {other_release_id} was not found"
	):
		store.release_move(release["id"], before_release_id=other_release_id)

	with pytest.raises(
		NotFoundError, match=f"release {other_release_id} was not found"
	):
		store.release_move(other_release_id, before_release_id=release["id"])


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
		_add_task(store, f"task-{number}", f"Task {number}", release_id=release["id"])
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


@pytest.mark.parametrize("overview", ["", " \t"])
def test_release_edit_rejects_blank_overview(tmp_path: Path, overview: str) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Overview")

	with pytest.raises(ProgressError, match="release overview"):
		store.release_edit(release["id"], overview=overview)


def test_release_edit_rejects_an_unchanged_overview(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Overview")

	with pytest.raises(ProgressError, match="already unchanged"):
		store.release_edit(release["id"], overview="Overview")


def test_release_edit_requires_one_overview_input(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")

	with pytest.raises(ProgressError, match="requires"):
		store.release_edit(release["id"])


@pytest.mark.parametrize("initial_status", ["planned", "active"])
def test_release_complete_moves_planned_or_active_releases_to_done(
	tmp_path: Path, initial_status: str
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add(
		"release",
		"Release",
		overview="Release overview",
		status=initial_status,
	)

	completed = store.release_complete(release["id"])

	assert completed["id"] == release["id"]
	assert completed["status"] == "done"


def test_release_complete_rejects_an_already_done_release(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add(
		"release", "Release", overview="Release overview", status="done"
	)

	with pytest.raises(InvalidTransitionError, match="done"):
		store.release_complete(release["id"])

	assert (
		ReadStore(store.database, _ProjectStore(store.database)).release_list(
			show_all=True
		)["items"][0]["status"]
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
			f"release-{number}",
			f"Release {number}",
			overview=f"Release {number} overview",
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
