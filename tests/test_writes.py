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
	NotFoundError,
	PendingChunksError,
	ProgressError,
	StillReferencedError,
	UnresolvedDependenciesError,
	WrongObjectIdTypeError,
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


def _add_task(store: WriteStore, slug: str, title: str, **arguments):
	"""Create a valid test task with default planning text."""
	arguments.setdefault("overview", f"{title} overview")
	arguments.setdefault("purpose", f"{title} purpose")
	arguments.setdefault("contract", f"{title} contract")
	return store.task_add(slug, title, **arguments)


def _add_chunk(
	store: WriteStore,
	task_id: str,
	title: str,
	description: str = "Chunk description",
	**arguments,
):
	"""Create a valid test chunk with a default description."""
	return store.chunk_add(task_id, title, description=description, **arguments)


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
		pytest.param({"purpose": ""}, id="empty-purpose"),
		pytest.param({"purpose": " \t"}, id="whitespace-purpose"),
		pytest.param({"contract": ""}, id="empty-contract"),
		pytest.param({"contract": " \t"}, id="whitespace-contract"),
	],
)
def test_task_add_rejects_blank_purpose_and_contract(
	tmp_path: Path, arguments: dict[str, str]
) -> None:
	store = _seed_store(tmp_path)

	with pytest.raises(ProgressError, match="task (purpose|contract)"):
		store.task_add(
			"task",
			"Task",
			overview="Task overview",
			purpose=arguments.get("purpose", "Task purpose"),
			contract=arguments.get("contract", "Task contract"),
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
		store.chunk_add(task["id"], "Chunk", **arguments)

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
	except Exception as error:
		results.put(("error", getattr(error, "code", type(error).__name__)))
	else:
		results.put(("ok", result["slug"]))


def test_explicit_zero_positions_are_preserved(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add(
		"zero-release", "Zero release", overview="Zero release overview", position=0
	)
	task = _add_task(
		store, "zero-task", "Zero task", release_id=release["id"], position=0
	)
	chunk = _add_chunk(store, task["id"], "Zero chunk", position=0)

	assert release["position"] == 0
	assert task["position"] == 0
	assert chunk["position"] == 0


def test_task_defaults_use_the_next_free_position_in_each_queue(
	tmp_path: Path,
) -> None:
	store = _seed_store(tmp_path)
	release = store.release_add("release", "Release", overview="Release overview")
	_add_task(store, "first", "First", release_id=release["id"], position=1)
	_add_task(store, "third", "Third", release_id=release["id"], position=3)
	_add_task(store, "unassigned-first", "Unassigned first", position=1)

	release_task = _add_task(store, "second", "Second", release_id=release["id"])
	unassigned_task = _add_task(store, "unassigned-second", "Unassigned second")

	assert release_task["position"] == 2
	assert unassigned_task["position"] == 2


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


def test_chunk_defaults_use_the_next_free_position(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "chunk-positions", "Chunk positions")
	first = _add_chunk(store, task["id"], "First", position=1)
	third = _add_chunk(store, task["id"], "Third", position=3)
	second = _add_chunk(store, task["id"], "Second")

	assert [first["position"], second["position"], third["position"]] == [1, 2, 3]


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
		purpose="Run lifecycle.",
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

	assert dependent["status"] == "blocked"
	assert "unresolved dependencies" in dependent["status_reason"]
	with pytest.raises(UnresolvedDependenciesError):
		store.task_unblock(dependent["id"])

	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	ready_dependent = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		dependent["id"]
	)

	assert ready_dependent["status"] == "ready"
	assert ready_dependent["status_reason"] is None


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
	assert blocked_dependent["status"] == "blocked"
	assert blocked_dependent["status_reason"] == status_reason


def test_starting_a_second_task_demotes_the_first_to_ready(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	first_chunk = _add_chunk(store, first["id"], "Chunk")
	second = _add_task(store, "second", "Second")
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
	task = _add_task(store, "only", "Only")

	started = store.task_start(task["id"])

	assert started["demoted_task"] is None


def test_demoting_a_task_preserves_its_completed_chunks(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	first = _add_task(store, "first", "First")
	completed_chunk = _add_chunk(store, first["id"], "Done chunk")
	remaining_chunk = _add_chunk(store, first["id"], "Remaining chunk")
	second = _add_task(store, "second", "Second")
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

	assert blocked["status"] == "blocked"
	with pytest.raises(InvalidTransitionError):
		store.task_start(task["id"])

	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	store.task_start(task["id"])
	second_dependency = _add_task(store, "second-dependency", "Second dependency")

	with pytest.raises(InvalidDependencyError):
		store.task_dependency_add(task["id"], second_dependency["id"])


def test_notes_and_context_replace_the_project_context_row(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "notes", "Notes")
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
	release = store.release_add("release", "Release", overview="Release overview")
	task = _add_task(store, "task", "Task", release_id=release["id"])
	chunk = _add_chunk(store, task["id"], "Chunk")
	dependent = _add_task(store, "dependent", "Dependent")
	dependency = _add_task(store, "dependency", "Dependency")
	store.task_dependency_add(dependent["id"], task["id"])
	store.task_dependency_add(task["id"], dependency["id"])
	discovery = store.discovery_add(task["id"], "Discovery")
	decision = store.decision_add(task["id"], "Decision")

	with pytest.raises(StillReferencedError, match=task["id"]):
		store.release_remove(release["id"])

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


def test_task_clean_force_removes_only_blocked_done_tasks_and_notes(
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
	store.task_start(dependency["id"])
	store.task_complete(dependency["id"])
	store.task_start(blocked_task["id"])
	store.task_complete(blocked_task["id"])

	store.task_clean()
	new_clean_task = _add_task(store, "new-clean", "New clean task")
	store.task_start(new_clean_task["id"])
	store.task_complete(new_clean_task["id"])

	result = store.task_clean(force=True)

	assert result["removed_count"] == 2
	assert {task["id"] for task in result["removed"]} == {
		dependency["id"],
		blocked_task["id"],
	}
	assert result["blocked"] == []
	assert result["releases_removed"] == [{"id": release["id"], "title": "Blocked"}]
	assert (
		ReadStore(store.database, _ProjectStore(store.database)).task_get(
			new_clean_task["id"]
		)["status"]
		== "done"
	)

	with store.database.connection() as connection:
		assert connection.execute("SELECT 1 FROM notes").fetchone() is None
		assert connection.execute("SELECT 1 FROM task_dependencies").fetchone() is None


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
		purpose="Original purpose",
		contract="Original contract",
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
	task = _add_task(store, "task", "Task", purpose="Original purpose")

	with pytest.raises(ProgressError, match="must be text"):
		store.task_edit(task["id"], overview="Updated overview", purpose=object())  # type: ignore[arg-type]

	current = ReadStore(store.database, _ProjectStore(store.database)).task_get(
		task["id"]
	)

	assert current == task


def test_task_edit_requires_at_least_one_field(tmp_path: Path) -> None:
	store = _seed_store(tmp_path)
	task = _add_task(store, "task", "Task")

	with pytest.raises(ProgressError, match="requires at least one field"):
		store.task_edit(task["id"])


@pytest.mark.parametrize(
	("field", "value"),
	[
		("overview", ""),
		("purpose", " \t"),
		("contract", ""),
	],
)
def test_task_edit_rejects_blank_required_text(
	tmp_path: Path, field: str, value: str
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
