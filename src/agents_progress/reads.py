"""Read queries shared by the human and JSON progress interfaces."""

from collections.abc import Callable, Sequence
from pathlib import Path
import sqlite3

from .errors import (
	InvalidObjectIdError,
	InvalidStatusError,
	NotFoundError,
	WrongObjectIdTypeError,
)
from .ids import (
	CHUNK_PREFIX,
	RELEASE_PREFIX,
	TASK_PREFIX,
	is_valid_object_id,
	validate_object_id,
)
from .models import Chunk, Context, Note, Release, Task
from .projects import Project, _StoreBase

# Default number of records returned per page.
DEFAULT_LIMIT = 50
# Maximum records allowed per page.
MAX_LIMIT = 200

# Task statuses accepted by the task list --status filter.
TASK_STATUSES = frozenset({"ready", "in-progress", "blocked", "needs-decision", "done"})

# Columns selected from releases in list and single-row queries.
_RELEASE_COLUMNS = "id, project_id, slug, title, overview, status, position"

# Columns selected from tasks in list and single-row queries.
_TASK_COLUMNS = (
	"id, project_id, slug, release_id, title, overview, purpose, contract, "
	"files, acceptance_criteria, verification, risks, status, "
	"status_reason, position, created_at, started_at, completed_at, updated_at"
)

# Qualified task columns for joins with tables that share column names.
_TASK_COLUMNS_QUALIFIED = ", ".join(
	f"tasks.{column.strip()}" for column in _TASK_COLUMNS.split(",")
)

# Queue order used by `next`. Unassigned tasks land in the final priority bucket,
# where their NULL release position sorts before done releases in that bucket.
_TASK_QUEUE_ORDER = (
	"CASE releases.status "
	"WHEN 'active' THEN 0 "
	"WHEN 'planned' THEN 1 "
	"ELSE 2 END, releases.position, tasks.position, tasks.id"
)

# Tables that support project-scoped slug lookup for command identifiers.
_IDENTIFIER_TABLES = {
	RELEASE_PREFIX: "releases",
	TASK_PREFIX: "tasks",
}

# Columns selected from chunks in list queries.
_CHUNK_COLUMNS = (
	"id, task_id, position, title, description, status, started_at, completed_at"
)

# Columns selected from notes in list queries.
_NOTE_COLUMNS = "id, project_id, task_id, type, body, supersedes_id, created_at"

# Columns selected from handoff context in read queries.
_CONTEXT_COLUMNS = (
	"project_id, current_goal, previous_step, next_step, standing_context, "
	"verify_with, stop_marker, updated_at"
)

# Note types accepted by the note commands.
NOTE_TYPES = frozenset({"discovery", "decision"})

# Fields checked by the doctor command for each record type.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
	"release": ("overview",),
	"task": ("overview", "purpose", "contract"),
	"chunk": ("description",),
}


def validate_identifier(value: str, expected_prefix: str) -> str:
	"""Validate an ID-shaped identifier, leaving slug candidates unchanged."""
	try:
		return validate_object_id(value, expected_prefix)
	except (InvalidObjectIdError, WrongObjectIdTypeError):
		if is_valid_object_id(value):
			raise

	return value


def resolve_identifier(
	connection: sqlite3.Connection,
	value: str,
	expected_prefix: str,
	project_id: str,
) -> str:
	"""Resolve a release or task ID or project slug.

	Return the matched ID, or return the raw value unchanged when no slug matches so the
	caller's not-found check handles it. Raise ValueError for an unsupported prefix.
	"""
	table = _IDENTIFIER_TABLES.get(expected_prefix)
	if table is None:
		raise ValueError(f"slug lookup is not supported for {expected_prefix!r}")

	value = validate_identifier(value, expected_prefix)
	if is_valid_object_id(value, expected_prefix):
		return value

	row = connection.execute(
		f"SELECT id FROM {table} WHERE project_id = ? AND slug = ?",
		(project_id, value),
	).fetchone()

	return value if row is None else row["id"]


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
	"""Build the bounded page response returned by list queries."""
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
	# Collect unique release IDs before the single project-scoped lookup.
	release_ids = list(
		dict.fromkeys(
			str(item["release_id"])
			for item in items
			if item.get("release_id") is not None
		)
	)
	if not release_ids:
		return

	# Build one parameter placeholder for each release ID.
	placeholders = ", ".join("?" for _ in release_ids)
	# Fetch all matching titles together to avoid one query per task.
	rows = connection.execute(
		f"SELECT id, title FROM releases WHERE project_id = ? AND id IN ({placeholders})",
		(project_id, *release_ids),
	).fetchall()
	# Index titles by release ID for response enrichment.
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

	def next(
		self,
		path: str | Path | None = None,
		*,
		include_position_totals: bool = False,
	) -> dict[str, object]:
		"""Return the next queued task, its active chunk, and a next-command hint.

		Set include_position_totals for human display: the response then
		also carries task_total and, when a chunk is active, chunk_total.
		"""
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

		response = _task_response(
			project, task_row, chunk_row, "progress task list", dependency_ids
		)
		if not include_position_totals or task_row is None:
			return response

		response["task_rank"] = self.task_rank_for_release(
			task_row["id"], task_row["release_id"], path
		)
		response["task_total"] = self.task_count_for_release(
			task_row["release_id"], path
		)
		if chunk_row is not None:
			response["chunk_rank"] = self.chunk_rank_for_task(
				chunk_row["id"], chunk_row["task_id"], path
			)
			response["chunk_total"] = self.chunk_count_for_task(
				chunk_row["task_id"], path
			)

		return response

	def doctor(self, path: str | Path | None = None) -> dict[str, object]:
		"""Report records with blank fields from the required-in-practice list."""
		# Bounded-list loader for each record type, used by the checks below.
		page_loaders: dict[str, Callable[[int, int], dict[str, object]]] = {
			"release": lambda limit, offset: self.release_list(
				limit, offset, path, show_all=True
			),
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
		*,
		show_all: bool = False,
		include_hidden_count: bool = False,
	) -> dict[str, object]:
		"""List the current project's releases in position order, one bounded page at a time.

		Only planned and active releases are listed by default. Pass show_all to include done
		releases as well. The human output passes include_hidden_count to add hidden_done_count
		to the result when done releases are hidden and at least one exists.
		"""
		limit, offset = validate_page(limit, offset)
		project = self.current_project(path)
		where = "project_id = ?"
		parameters = (project.id,)

		if not show_all:
			where += " AND status IN ('planned', 'active')"

		with self.database.connection() as connection:
			response = self._paged_query(
				connection,
				f"SELECT {_RELEASE_COLUMNS} FROM releases "
				f"WHERE {where} ORDER BY position, id",
				parameters,
				Release.from_row,
				limit,
				offset,
				"releases",
				where,
				parameters,
			)

			if include_hidden_count and not show_all:
				hidden_done_count = connection.execute(
					"SELECT COUNT(*) FROM releases "
					"WHERE project_id = ? AND status = 'done'",
					(project.id,),
				).fetchone()[0]
				if hidden_done_count > 0:
					response["hidden_done_count"] = hidden_done_count

			return response

	def release_get(
		self,
		release_id: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Return one current-project release by ID or project slug, or raise not-found."""
		release_id = validate_identifier(release_id, RELEASE_PREFIX)
		project = self.current_project(path)

		with self.database.connection() as connection:
			release_id = resolve_identifier(
				connection, release_id, RELEASE_PREFIX, project.id
			)
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
		"""Return one current-project task by ID or project slug, or raise not-found."""
		task_id = validate_identifier(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.connection() as connection:
			task_id = resolve_identifier(connection, task_id, TASK_PREFIX, project.id)
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

	def task_count_for_release(
		self, release_id: str | None, path: str | Path | None = None
	) -> int:
		"""Count the current project's tasks under one release, including a null release_id."""
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				"SELECT COUNT(*) AS total FROM tasks "
				"WHERE project_id = ? AND release_id IS ?",
				(project.id, release_id),
			).fetchone()

		return int(row["total"])

	def task_rank_for_release(
		self,
		task_id: str,
		release_id: str | None,
		path: str | Path | None = None,
	) -> int:
		"""Rank a task 1-based among its release's tasks, ordered by position then id.

		Unlike the stored position column, this rank stays contiguous even
		after sibling tasks are removed.
		"""
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				"SELECT position_rank FROM ("
				"SELECT id, ROW_NUMBER() OVER (ORDER BY position, id) AS position_rank "
				"FROM tasks WHERE project_id = ? AND release_id IS ?"
				") WHERE id = ?",
				(project.id, release_id, task_id),
			).fetchone()

		return int(row["position_rank"])

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

	def chunk_count_for_task(self, task_id: str, path: str | Path | None = None) -> int:
		"""Count one task's chunks, scoped to the current project via its parent task."""
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				"SELECT COUNT(*) AS total FROM chunks "
				"WHERE task_id = ? AND EXISTS ("
				"SELECT 1 FROM tasks WHERE tasks.id = chunks.task_id "
				"AND tasks.project_id = ?)",
				(task_id, project.id),
			).fetchone()

		return int(row["total"])

	def chunk_rank_for_task(
		self,
		chunk_id: str,
		task_id: str,
		path: str | Path | None = None,
	) -> int:
		"""Rank a chunk 1-based among its task's chunks, ordered by position then id.

		Scoped to the current project via its parent task, same as chunk_count_for_task.
		"""
		project = self.current_project(path)

		with self.database.connection() as connection:
			row = connection.execute(
				"SELECT position_rank FROM ("
				"SELECT id, ROW_NUMBER() OVER (ORDER BY position, id) AS position_rank "
				"FROM chunks WHERE task_id = ? AND EXISTS ("
				"SELECT 1 FROM tasks WHERE tasks.id = chunks.task_id "
				"AND tasks.project_id = ?"
				")"
				") WHERE id = ?",
				(task_id, project.id, chunk_id),
			).fetchone()

		return int(row["position_rank"])

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
