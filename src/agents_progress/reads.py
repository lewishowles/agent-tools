"""Read queries shared by the human and JSON progress interfaces."""

from collections.abc import Callable, Sequence
from pathlib import Path
import sqlite3

from .errors import InvalidStatusError, NotFoundError
from .ids import (
	CHUNK_PREFIX,
	RELEASE_PREFIX,
	TASK_PREFIX,
	validate_object_id,
)
from .models import Chunk, Context, Note, Release, Task
from .projects import Project, _StoreBase

# Default and maximum page size for bounded list responses.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Task statuses accepted by the task list --status filter.
TASK_STATUSES = frozenset({"ready", "in-progress", "blocked", "needs-decision", "done"})

# Column lists shared between single-row and paged queries per table.
_RELEASE_COLUMNS = "id, project_id, slug, title, overview, status, position"

_TASK_COLUMNS = (
	"id, project_id, slug, release_id, title, overview, purpose, contract, "
	"model_tier, files, acceptance_criteria, verification, risks, status, "
	"status_reason, position, created_at, started_at, completed_at, updated_at"
)

# _TASK_COLUMNS qualified with the tasks table alias, for queries that join
# another table sharing column names (e.g. releases).
_TASK_COLUMNS_QUALIFIED = ", ".join(
	f"tasks.{column.strip()}" for column in _TASK_COLUMNS.split(",")
)

# Unassigned tasks use the final release-priority bucket. Their NULL release
# position sorts before done releases in that bucket, matching `next`.
_TASK_QUEUE_ORDER = (
	"CASE releases.status "
	"WHEN 'active' THEN 0 "
	"WHEN 'planned' THEN 1 "
	"ELSE 2 END, releases.position, tasks.position, tasks.id"
)

_CHUNK_COLUMNS = (
	"id, task_id, position, title, description, status, started_at, completed_at"
)

_NOTE_COLUMNS = "id, project_id, task_id, type, body, supersedes_id, created_at"

_CONTEXT_COLUMNS = (
	"project_id, current_goal, previous_step, next_step, standing_context, "
	"verify_with, stop_marker, updated_at"
)

NOTE_TYPES = frozenset({"discovery", "decision"})

# Fields that should be populated for each record in the doctor check.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
	"release": ("overview",),
	"task": ("overview", "purpose", "contract"),
	"chunk": ("description",),
}


def validate_page(limit: int = DEFAULT_LIMIT, offset: int = 0) -> tuple[int, int]:
	"""Reject an out-of-range limit or offset and return the validated pair."""
	if not 1 <= limit <= MAX_LIMIT:
		raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
	if offset < 0:
		raise ValueError("offset must be zero or greater")

	return limit, offset


def page_response(
	items: Sequence[dict[str, object]], limit: int, offset: int, total: int
) -> dict[str, object]:
	return {
		"items": list(items),
		"limit": limit,
		"offset": offset,
		"has_more": offset + len(items) < total,
	}


def _all_pages(
	page_loader: Callable[[int, int], dict[str, object]],
) -> list[dict[str, object]]:
	"""Read every page from a bounded list query."""
	records: list[dict[str, object]] = []
	offset = 0

	while True:
		page = page_loader(MAX_LIMIT, offset)
		items = page.get("items", [])
		if isinstance(items, list):
			records.extend(item for item in items if isinstance(item, dict))

		if not page.get("has_more"):
			break

		offset += int(page.get("limit", MAX_LIMIT))

	return records


def _is_blank(value: object) -> bool:
	"""Return whether a required field is missing or contains only whitespace."""
	return value is None or (isinstance(value, str) and not value.strip())


def _add_release_titles(
	connection: sqlite3.Connection,
	project_id: str,
	items: list[dict[str, object]],
) -> None:
	"""Add the matching release title to each task-list item with one bounded query."""
	release_ids = list(
		dict.fromkeys(
			str(item["release_id"])
			for item in items
			if item.get("release_id") is not None
		)
	)
	if not release_ids:
		return

	placeholders = ", ".join("?" for _ in release_ids)
	rows = connection.execute(
		f"SELECT id, title FROM releases WHERE project_id = ? AND id IN ({placeholders})",
		(project_id, *release_ids),
	).fetchall()
	titles = {str(row["id"]): str(row["title"]) for row in rows}

	for item in items:
		release_id = item.get("release_id")
		if release_id is not None:
			item["release_title"] = titles.get(str(release_id))


def _task_response(
	project: Project,
	task_row: sqlite3.Row | None,
	chunk_row: sqlite3.Row | None,
	empty_hint: str,
	dependency_ids: Sequence[str],
) -> dict[str, object]:
	"""Build the stable response used by the next command."""
	task = Task.from_row(task_row) if task_row is not None else None
	chunk = Chunk.from_row(chunk_row) if chunk_row is not None else None

	return {
		"project": project.to_dict(),
		"task": task.to_dict() if task is not None else None,
		"chunk": chunk.to_dict() if chunk is not None else None,
		"dependency_ids": list(dependency_ids),
		"hint_command": ReadStore._next_hint(task, chunk, empty_hint),
	}


def _in_progress_task_and_chunk(
	connection: sqlite3.Connection, project_id: str
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
	"""Fetch the current in-progress task and its active chunk."""
	task_row = connection.execute(
		f"SELECT {_TASK_COLUMNS} FROM tasks "
		"WHERE project_id = ? AND status = 'in-progress'",
		(project_id,),
	).fetchone()
	chunk_row = (
		_active_chunk(connection, task_row["id"]) if task_row is not None else None
	)

	return task_row, chunk_row


def _active_chunk(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
	"""Fetch the first active chunk for a task."""
	return connection.execute(
		f"SELECT {_CHUNK_COLUMNS} FROM chunks "
		"WHERE task_id = ? AND status = 'active' "
		"ORDER BY position, id LIMIT 1",
		(task_id,),
	).fetchone()


def _dependency_ids(connection: sqlite3.Connection, task_id: str) -> list[str]:
	"""Return a task's dependency IDs in deterministic order."""
	rows = connection.execute(
		"SELECT depends_on_task_id FROM task_dependencies "
		"WHERE task_id = ? ORDER BY depends_on_task_id",
		(task_id,),
	).fetchall()

	return [row["depends_on_task_id"] for row in rows]


class ReadStore(_StoreBase):
	"""Run the current project's read queries against the progress database."""

	def next(self, path: str | Path | None = None) -> dict[str, object]:
		"""Return the next queued task, its active chunk, and a next-command hint."""
		project = self.current_project(path)

		with self.database.connection() as connection:
			task_row, chunk_row = _in_progress_task_and_chunk(connection, project.id)
			if task_row is None:
				# An active release's tasks outrank a planned release's, and
				# release position breaks ties before falling back to task order.
				task_row = connection.execute(
					f"SELECT {_TASK_COLUMNS_QUALIFIED} FROM tasks "
					"LEFT JOIN releases ON releases.id = tasks.release_id "
					"AND releases.project_id = tasks.project_id "
					"WHERE tasks.project_id = ? "
					"AND tasks.status IN ('ready', 'blocked', 'needs-decision') "
					f"ORDER BY {_TASK_QUEUE_ORDER} LIMIT 1",
					(project.id,),
				).fetchone()
				if task_row is not None:
					chunk_row = _active_chunk(connection, task_row["id"])
			dependency_ids = (
				_dependency_ids(connection, task_row["id"])
				if task_row is not None
				else []
			)

		return _task_response(
			project, task_row, chunk_row, "progress task list", dependency_ids
		)

	def doctor(self, path: str | Path | None = None) -> dict[str, object]:
		"""Report records with blank fields from the required-in-practice list."""
		page_loaders: dict[str, Callable[[int, int], dict[str, object]]] = {
			"release": lambda limit, offset: self.release_list(limit, offset, path),
			"task": lambda limit, offset: self.task_list(None, limit, offset, path),
			"chunk": lambda limit, offset: self._chunk_list_for_project(
				limit, offset, path
			),
		}
		findings: list[dict[str, object]] = []

		for noun, fields in REQUIRED_FIELDS.items():
			for record in _all_pages(page_loaders[noun]):
				for field in fields:
					if _is_blank(record.get(field)):
						findings.append(
							{
								"field": f"{noun}.{field}",
								"id": record.get("id"),
								"noun": noun,
								"title": record.get("title"),
							}
						)

		return {"findings": findings, "ok": not findings}

	def _chunk_list_for_project(
		self,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List every chunk in the current project for cross-task checks."""
		limit, offset = validate_page(limit, offset)
		project = self.current_project(path)
		where = "task_id IN (SELECT id FROM tasks WHERE project_id = ?)"

		with self.database.connection() as connection:
			return self._paged_query(
				connection,
				f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE {where} "
				"ORDER BY position, id",
				(project.id,),
				Chunk.from_row,
				limit,
				offset,
				"chunks",
				where,
				(project.id,),
			)

	def release_list(
		self,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List the current project's releases in position order, one bounded page at a time."""
		limit, offset = validate_page(limit, offset)
		project = self.current_project(path)

		with self.database.connection() as connection:
			return self._paged_query(
				connection,
				f"SELECT {_RELEASE_COLUMNS} FROM releases "
				"WHERE project_id = ? ORDER BY position, id",
				(project.id,),
				Release.from_row,
				limit,
				offset,
				"releases",
				"project_id = ?",
				(project.id,),
			)

	def release_get(
		self,
		release_id: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Return one current-project release, raising not-found if it does not exist here."""
		validate_object_id(release_id, RELEASE_PREFIX)
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				f"SELECT {_RELEASE_COLUMNS} FROM releases "
				"WHERE id = ? AND project_id = ?",
				(release_id, project.id),
			).fetchone()

		if row is None:
			raise NotFoundError(
				f"release {release_id} was not found", {"id": release_id}
			)

		return Release.from_row(row).to_dict()

	def task_get(
		self,
		task_id: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Return one current-project task, raising not-found if it does not exist here."""
		validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ? AND project_id = ?",
				(task_id, project.id),
			).fetchone()

		if row is None:
			raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

		return Task.from_row(row).to_dict()

	def task_list(
		self,
		status: str | None = None,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
		*,
		include_release_titles: bool = False,
	) -> dict[str, object]:
		"""List tasks in the same release-priority order used by `next`."""
		limit, offset = validate_page(limit, offset)
		if status is not None and status not in TASK_STATUSES:
			raise InvalidStatusError(
				f"unknown task status {status!r}",
				{"status": status, "valid_statuses": sorted(TASK_STATUSES)},
			)

		project = self.current_project(path)
		where = "tasks.project_id = ?"
		parameters: tuple[object, ...] = (project.id,)
		if status is not None:
			where += " AND tasks.status = ?"
			parameters += (status,)

		with self.database.connection() as connection:
			response = self._paged_query(
				connection,
				f"SELECT {_TASK_COLUMNS_QUALIFIED} FROM tasks "
				"LEFT JOIN releases ON releases.id = tasks.release_id "
				"AND releases.project_id = tasks.project_id "
				f"WHERE {where} ORDER BY {_TASK_QUEUE_ORDER}",
				parameters,
				Task.from_row,
				limit,
				offset,
				"tasks",
				where,
				parameters,
			)
			if include_release_titles:
				items = response["items"]
				if isinstance(items, list):
					_add_release_titles(connection, project.id, items)

			return response

	def chunk_get(
		self,
		chunk_id: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Return one current-project chunk, raising not-found if it does not exist here."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				f"SELECT {_CHUNK_COLUMNS} FROM chunks "
				"WHERE id = ? AND EXISTS ("
				"SELECT 1 FROM tasks WHERE tasks.id = chunks.task_id "
				"AND tasks.project_id = ?)",
				(chunk_id, project.id),
			).fetchone()

		if row is None:
			raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})

		return Chunk.from_row(row).to_dict()

	def chunk_list(
		self,
		task_id: str,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List one current-project task's chunks in position order."""
		validate_object_id(task_id, TASK_PREFIX)
		limit, offset = validate_page(limit, offset)
		project = self.current_project(path)

		with self.database.connection() as connection:
			task_exists = connection.execute(
				"SELECT 1 FROM tasks WHERE id = ? AND project_id = ?",
				(task_id, project.id),
			).fetchone()
			if task_exists is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			return self._paged_query(
				connection,
				f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE task_id = ? "
				"ORDER BY position, id",
				(task_id,),
				Chunk.from_row,
				limit,
				offset,
				"chunks",
				"task_id = ?",
				(task_id,),
			)

	def discovery_list(
		self,
		task_id: str | None = None,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List discovery notes for the current project, optionally for one task."""
		return self.note_list("discovery", task_id, limit, offset, path)

	def decision_list(
		self,
		task_id: str | None = None,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List decision notes for the current project, optionally for one task."""
		return self.note_list("decision", task_id, limit, offset, path)

	def note_list(
		self,
		note_type: str,
		task_id: str | None = None,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List one type of note for the current project in creation order."""
		if note_type not in NOTE_TYPES:
			raise ValueError(f"unknown note type {note_type!r}")
		if task_id is not None:
			validate_object_id(task_id, TASK_PREFIX)

		limit, offset = validate_page(limit, offset)
		project = self.current_project(path)
		where = "project_id = ? AND type = ?"
		parameters: tuple[object, ...] = (project.id, note_type)
		if task_id is not None:
			where += " AND task_id = ?"
			parameters += (task_id,)

		with self.database.connection() as connection:
			return self._paged_query(
				connection,
				f"SELECT {_NOTE_COLUMNS} FROM notes WHERE {where} "
				"ORDER BY created_at, id",
				parameters,
				Note.from_row,
				limit,
				offset,
				"notes",
				where,
				parameters,
			)

	def context_get(self, path: str | Path | None = None) -> dict[str, object]:
		"""Return the current project's handoff context or a clear not-set result."""
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				f"SELECT {_CONTEXT_COLUMNS} FROM context WHERE project_id = ?",
				(project.id,),
			).fetchone()

		if row is None:
			return {"status": "not-set", "project_id": project.id}

		return Context.from_row(row).to_dict()

	def _paged_query(
		self,
		connection: sqlite3.Connection,
		query: str,
		parameters: tuple[object, ...],
		factory: Callable[[object], Release | Task | Chunk | Note],
		limit: int,
		offset: int,
		table: str,
		where: str,
		count_parameters: tuple[object, ...],
	) -> dict[str, object]:
		"""Run one ordered query with a LIMIT/OFFSET window and its matching total count."""
		rows = connection.execute(
			f"{query} LIMIT ? OFFSET ?",
			(*parameters, limit, offset),
		).fetchall()
		total = connection.execute(
			f"SELECT COUNT(*) FROM {table} WHERE {where}", count_parameters
		).fetchone()[0]
		items = [factory(row).to_dict() for row in rows]

		return page_response(items, limit, offset, total)

	@staticmethod
	def _next_hint(
		task: Task | None, chunk: Chunk | None, empty_hint: str
	) -> str | None:
		"""Suggest the next useful command for the selected task and chunk state."""
		if task is None:
			return empty_hint
		if task.status == "ready":
			return f"progress task start {task.id}"
		if task.status in {"blocked", "needs-decision"}:
			return f"progress task unblock {task.id}"
		if chunk is not None:
			return f"progress chunk complete {chunk.id}"
		return f"progress task complete {task.id}"
