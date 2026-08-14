"""Read queries shared by the human and JSON progress interfaces."""

from collections.abc import Callable, Sequence
from pathlib import Path
import sqlite3

from .errors import InvalidStatusError, NotFoundError
from .ids import TASK_PREFIX, validate_object_id
from .models import Chunk, Release, Task
from .projects import _StoreBase

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

_CHUNK_COLUMNS = (
	"id, task_id, position, title, description, status, started_at, completed_at"
)


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


class ReadStore(_StoreBase):
	"""Run the current project's read queries against the progress database."""

	def next(self, path: str | Path | None = None) -> dict[str, object]:
		"""Return the current project's in-progress task, its active chunk, and a next-command hint."""
		project = self.current_project(path)

		with self.database.connection() as connection:
			task_row = connection.execute(
				f"SELECT {_TASK_COLUMNS} FROM tasks "
				"WHERE project_id = ? AND status = 'in-progress'",
				(project.id,),
			).fetchone()
			chunk_row = None
			if task_row is not None:
				chunk_row = connection.execute(
					f"SELECT {_CHUNK_COLUMNS} FROM chunks "
					"WHERE task_id = ? AND status = 'active' "
					"ORDER BY position, id LIMIT 1",
					(task_row["id"],),
				).fetchone()

		task = Task.from_row(task_row) if task_row is not None else None
		chunk = Chunk.from_row(chunk_row) if chunk_row is not None else None
		hint_command = self._next_hint(task, chunk)

		return {
			"project": project.to_dict(),
			"task": task.to_dict() if task is not None else None,
			"chunk": chunk.to_dict() if chunk is not None else None,
			"hint_command": hint_command,
		}

	def current(self, path: str | Path | None = None) -> dict[str, object]:
		"""Return the same read model as next; current and next answer the same question."""
		return self.next(path)

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
	) -> dict[str, object]:
		"""List the current project's tasks in position order, optionally filtered by status."""
		limit, offset = validate_page(limit, offset)
		if status is not None and status not in TASK_STATUSES:
			raise InvalidStatusError(
				f"unknown task status {status!r}",
				{"status": status, "valid_statuses": sorted(TASK_STATUSES)},
			)

		project = self.current_project(path)
		where = "project_id = ?"
		parameters: tuple[object, ...] = (project.id,)
		if status is not None:
			where += " AND status = ?"
			parameters += (status,)

		with self.database.connection() as connection:
			return self._paged_query(
				connection,
				f"SELECT {_TASK_COLUMNS} FROM tasks WHERE {where} "
				"ORDER BY position, id",
				parameters,
				Task.from_row,
				limit,
				offset,
				"tasks",
				where,
				parameters,
			)

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

	def ready(
		self,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List the current project's ready tasks whose dependencies are all done."""
		limit, offset = validate_page(limit, offset)
		project = self.current_project(path)
		where = (
			"project_id = ? AND status = 'ready' AND NOT EXISTS ("
			"SELECT 1 FROM task_dependencies AS dependencies "
			"JOIN tasks AS dependency ON dependency.id = dependencies.depends_on_task_id "
			"WHERE dependencies.task_id = tasks.id AND dependency.status != 'done'"
			")"
		)

		with self.database.connection() as connection:
			return self._paged_query(
				connection,
				f"SELECT {_TASK_COLUMNS} FROM tasks WHERE {where} "
				"ORDER BY position, id",
				(project.id,),
				Task.from_row,
				limit,
				offset,
				"tasks",
				where,
				(project.id,),
			)

	def _paged_query(
		self,
		connection: sqlite3.Connection,
		query: str,
		parameters: tuple[object, ...],
		factory: Callable[[object], Release | Task | Chunk],
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
	def _next_hint(task: Task | None, chunk: Chunk | None) -> str | None:
		"""Suggest the next useful command for the current task and chunk state."""
		if task is None:
			return "progress ready"
		if chunk is not None:
			return f"progress chunk complete {chunk.id}"
		return f"progress task complete {task.id}"
