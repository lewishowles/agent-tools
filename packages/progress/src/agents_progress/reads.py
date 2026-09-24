"""Read queries shared by the human and JSON progress interfaces."""

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

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
TASK_STATUSES = frozenset(
	{"ready", "waiting", "in-progress", "blocked", "needs-decision", "done"}
)

# Columns selected from releases in list and single-row queries.
_RELEASE_COLUMNS = "id, project_id, slug, title, overview, status, position"

# Columns selected from tasks in list and single-row queries.
_TASK_COLUMNS = (
	"id, project_id, slug, release_id, title, overview, verification, "
	"split_rationale, status, "
	"status_reason, position, created_at, started_at, completed_at, updated_at"
)

# Qualified task columns for joins with tables that share column names.
_TASK_COLUMNS_QUALIFIED = ", ".join(
	f"tasks.{column.strip()}" for column in _TASK_COLUMNS.split(",")
)

# Task fields stored as ordered rows rather than columns, mapped to the table holding them.
_TASK_LIST_TABLES = {
	"contract": "task_contract_steps",
	"files": "task_files",
}

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
_NOTE_COLUMNS = (
	"id, project_id, task_id, release_id, type, body, supersedes_id, created_at"
)

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
	"task": ("overview", "contract", "split_rationale"),
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
	"""Return whether a required field is missing, blank, or a list with nothing in it."""
	if value is None:
		return True

	if isinstance(value, str):
		return not value.strip()

	if isinstance(value, Sequence):
		return not value or all(_is_blank(item) for item in value)

	return False


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


def _task_values_by_id(
	connection: sqlite3.Connection,
	task_ids: Sequence[str],
) -> dict[str, dict[str, list[str]]]:
	"""Load the ordered contract steps and files for several tasks, one query per field.

	Returns each field mapped to the tasks that have values, in position order. A task with
	no rows for a field is absent rather than present with an empty list.
	"""
	values: dict[str, dict[str, list[str]]] = {field: {} for field in _TASK_LIST_TABLES}
	if not task_ids:
		return values

	placeholders = ", ".join("?" for _ in task_ids)
	for field, table in _TASK_LIST_TABLES.items():
		rows = connection.execute(
			f"SELECT task_id, text FROM {table} WHERE task_id IN ({placeholders}) "
			"ORDER BY task_id, position",
			tuple(task_ids),
		).fetchall()
		for row in rows:
			values[field].setdefault(row["task_id"], []).append(row["text"])

	return values


def _task_chunks(
	connection: sqlite3.Connection, task_id: str
) -> list[dict[str, object]]:
	"""Read one task's chunks in the order they are worked through."""
	chunk_rows = connection.execute(
		f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE task_id = ? ORDER BY position, id",
		(task_id,),
	).fetchall()

	return [Chunk.from_row(row).to_dict() for row in chunk_rows]


def _task_public_row(
	connection: sqlite3.Connection,
	task_row: object,
	values: dict[str, dict[str, list[str]]] | None = None,
) -> dict[str, object]:
	"""Return a task row as a dictionary, with its list fields read from the ordered tables.

	Pass values when building a page of tasks so each field is queried once for the whole
	page instead of once per row.
	"""
	data = dict(task_row)
	task_id = str(data["id"])
	values = values or _task_values_by_id(connection, [task_id])
	for field in _TASK_LIST_TABLES:
		data[field] = values[field].get(task_id, [])

	return data


def _release_public_data(
	connection: sqlite3.Connection, release_row: object
) -> dict[str, object]:
	"""Return a release with the notes that belong to it."""
	release = Release.from_row(release_row).to_dict()
	release_id = str(release["id"])
	project_id = str(release["project_id"])
	release["notes"] = [
		Note.from_row(row).to_dict()
		for row in connection.execute(
			f"SELECT {_NOTE_COLUMNS} FROM notes "
			"WHERE project_id = ? AND release_id = ? ORDER BY created_at, id",
			(project_id, release_id),
		).fetchall()
	]

	return release


def _task_response(
	connection: sqlite3.Connection,
	project: Project,
	task_row: sqlite3.Row | None,
	chunk_row: sqlite3.Row | None,
	empty_hint: str,
	dependency_ids: Sequence[str],
) -> dict[str, object]:
	"""Build the stable response used by the next command."""
	task = (
		Task.from_row(_task_public_row(connection, task_row))
		if task_row is not None
		else None
	)
	chunk = Chunk.from_row(chunk_row) if chunk_row is not None else None
	task_data = task.to_dict() if task is not None else None
	if task_data is not None:
		task_data["chunks"] = _task_chunks(connection, task.id)
	release_data = None
	if task is not None and task.release_id is not None:
		release_row = connection.execute(
			f"SELECT {_RELEASE_COLUMNS} FROM releases WHERE id = ? AND project_id = ?",
			(task.release_id, task.project_id),
		).fetchone()
		if release_row is not None:
			release_data = _release_public_data(connection, release_row)
	# The first unfinished dependency of a waiting task, so the hint can show
	# what the task is waiting for. IDs are sorted to keep the hint stable.
	unfinished_dependency_id = None
	if task is not None and task.status == "waiting":
		unfinished_dependency = connection.execute(
			"SELECT tasks.id FROM task_dependencies "
			"JOIN tasks ON tasks.id = task_dependencies.depends_on_task_id "
			"WHERE task_dependencies.task_id = ? AND tasks.status != 'done' "
			"ORDER BY tasks.id LIMIT 1",
			(task.id,),
		).fetchone()
		if unfinished_dependency is not None:
			unfinished_dependency_id = unfinished_dependency["id"]

	return {
		"project": project.to_dict(),
		"release": release_data,
		"task": task_data,
		"chunk": chunk.to_dict() if chunk is not None else None,
		"dependency_ids": list(dependency_ids),
		"hint_command": ReadStore._next_hint(
			task, chunk, empty_hint, unfinished_dependency_id
		),
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


def _search_like_pattern(term: str) -> str:
	"""Wrap the term in LIKE wildcards, escaping any % or _ the user typed so they match literally."""
	escaped_term = term.replace("\\", "\\\\")
	escaped_term = escaped_term.replace("%", "\\%")
	escaped_term = escaped_term.replace("_", "\\_")

	return f"%{escaped_term}%"


def _search_value_matches(value: object, term: str) -> bool:
	"""Report whether a stored value is text that mentions the term, ignoring case."""
	return isinstance(value, str) and term.lower() in value.lower()


def _search_snippet(text: str, term: str) -> str | None:
	"""Cut a short excerpt around the term's first appearance, or None when it is absent.

	The window is about 40 characters each side, trimmed to whole words when a space falls
	inside it and kept as-is otherwise, so a long path or URL still shows some context.
	An ellipsis marks each side that was cut.
	"""
	term_position = text.lower().find(term.lower())
	if term_position < 0:
		return None

	term_end = term_position + len(term)
	context_start = max(0, term_position - 40)
	context_end = min(len(text), term_end + 40)

	if context_start > 0:
		boundary_position = next(
			(
				position
				for position in range(context_start, term_position)
				if text[position].isspace()
			),
			None,
		)
		if boundary_position is not None:
			context_start = boundary_position + 1

	if context_end < len(text):
		boundary_position = next(
			(
				position
				for position in range(context_end - 1, term_end - 1, -1)
				if text[position].isspace()
			),
			None,
		)
		if boundary_position is not None:
			context_end = boundary_position

	return (
		("..." if context_start > 0 else "")
		+ text[context_start:context_end]
		+ ("..." if context_end < len(text) else "")
	)


def _search_where_clause(
	condition_map: dict[str, str],
	selected_fields: Sequence[str],
	project_id: str,
	status: str | None,
	like_pattern: str,
) -> tuple[str, tuple[object, ...]]:
	"""Build the WHERE clause and its parameters shared by the task and chunk queries.

	The clause scopes to the project, optionally the task status, then ORs one LIKE
	condition per selected field. Parameters come back in the same order as the
	placeholders, with the pattern repeated once per field.
	"""
	where = ["tasks.project_id = ?"]
	parameters: tuple[object, ...] = (project_id,)

	if status is not None:
		where.append("tasks.status = ?")
		parameters += (status,)

	where.append(
		"(" + " OR ".join(condition_map[field] for field in selected_fields) + ")"
	)
	parameters += (like_pattern,) * len(selected_fields)

	return " AND ".join(where), parameters


def _search_task_item(
	connection: sqlite3.Connection,
	row: sqlite3.Row,
	selected_fields: Sequence[str],
	term: str,
) -> dict[str, object]:
	"""Turn one matching task row into a search result item.

	Contract steps and file paths are loaded from their child tables when selected.
	File matches list every matching path; a contract match shows an excerpt of the
	first matching step; the title gets no excerpt because the item already shows it.
	"""
	child_values: dict[str, list[str]] = {}
	for field, table in _TASK_LIST_TABLES.items():
		if field not in selected_fields:
			continue
		child_values[field] = [
			str(child_row["text"])
			for child_row in connection.execute(
				f"SELECT text FROM {table} WHERE task_id = ? ORDER BY position",
				(row["id"],),
			).fetchall()
		]

	task_values = {
		"title": row["title"],
		"overview": row["overview"],
		"verification": row["verification"],
	}
	matched: list[str] = []
	snippets: dict[str, object] = {}

	for field in selected_fields:
		if field in child_values:
			matching_values = [
				value
				for value in child_values[field]
				if _search_value_matches(value, term)
			]
			if not matching_values:
				continue
			matched.append(field)
			if field == "files":
				snippets[field] = matching_values
			else:
				snippet = _search_snippet(matching_values[0], term)
				if snippet is not None:
					snippets[field] = snippet
			continue

		value = task_values[field]
		if not _search_value_matches(value, term):
			continue
		matched.append(field)
		if field != "title":
			snippet = _search_snippet(str(value), term)
			if snippet is not None:
				snippets[field] = snippet

	return {
		"type": "task",
		"id": row["id"],
		"title": row["title"],
		"status": row["status"],
		"matched": matched,
		"snippets": snippets,
	}


def _search_chunk_item(
	row: sqlite3.Row,
	selected_fields: Sequence[str],
	term: str,
) -> dict[str, object]:
	"""Turn one matching chunk row into a search result item, with the parent task named for context."""
	chunk_values = {
		"title": row["title"],
		"description": row["description"],
	}
	matched: list[str] = []
	snippets: dict[str, object] = {}
	for field in selected_fields:
		value = chunk_values[field]
		if not _search_value_matches(value, term):
			continue
		matched.append(field)
		if field != "title":
			snippet = _search_snippet(str(value), term)
			if snippet is not None:
				snippets[field] = snippet

	return {
		"type": "chunk",
		"id": row["id"],
		"title": row["title"],
		"status": row["status"],
		"task_id": row["task_id"],
		"task_title": row["task_title"],
		"matched": matched,
		"snippets": snippets,
	}


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
					"AND tasks.status IN ('ready', 'waiting', 'blocked', 'needs-decision') "
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
				connection,
				project,
				task_row,
				chunk_row,
				"progress task list",
				dependency_ids,
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

			return _release_public_data(connection, row)

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

			task = Task.from_row(_task_public_row(connection, row)).to_dict()
			task["chunks"] = _task_chunks(connection, task_id)

			return task

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
			rows = connection.execute(
				f"SELECT {_TASK_COLUMNS_QUALIFIED} FROM tasks "
				"LEFT JOIN releases ON releases.id = tasks.release_id "
				"AND releases.project_id = tasks.project_id "
				f"WHERE {where} ORDER BY {_TASK_QUEUE_ORDER} LIMIT ? OFFSET ?",
				(*parameters, limit, offset),
			).fetchall()
			total = connection.execute(
				f"SELECT COUNT(*) FROM tasks WHERE {where}", parameters
			).fetchone()[0]
			task_values = _task_values_by_id(
				connection, [str(row["id"]) for row in rows]
			)
			response = page_response(
				[
					Task.from_row(
						_task_public_row(connection, row, task_values)
					).to_dict()
					for row in rows
				],
				limit,
				offset,
				total,
			)
			if include_release_titles:
				items = response["items"]
				if isinstance(items, list):
					_add_release_titles(connection, project.id, items)

			return response

	def search(
		self,
		term: str,
		fields: Sequence[str] | None = None,
		status: str | None = None,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""List current-project tasks and chunks whose text mentions the term.

		Each item names the fields that matched and, for each matched field other than the
		title, a short excerpt; a files match lists the matching paths instead. `fields`
		limits which searchable fields are checked; None or an empty sequence means all of
		them. `status` keeps tasks with that status and chunks whose parent task has it.
		Tasks come first, then chunks, and both share one page.
		"""
		limit, offset = validate_page(limit, offset)
		if status is not None and status not in TASK_STATUSES:
			raise InvalidStatusError(
				f"unknown task status {status!r}",
				{"status": status, "valid_statuses": sorted(TASK_STATUSES)},
			)

		task_fields = (
			"title",
			"overview",
			"verification",
			"contract",
			"files",
		)
		all_fields = (*task_fields, "description")
		requested_fields = set(all_fields if not fields else fields)
		unknown_fields = requested_fields.difference(all_fields)
		if unknown_fields:
			raise ValueError(f"unknown search field {min(unknown_fields)!r}")

		selected_fields = tuple(
			field for field in all_fields if field in requested_fields
		)
		like_pattern = _search_like_pattern(term)
		project = self.current_project(path)

		with self.database.connection() as connection:
			task_conditions = {
				"title": "tasks.title LIKE ? ESCAPE '\\'",
				"overview": "tasks.overview LIKE ? ESCAPE '\\'",
				"verification": "tasks.verification LIKE ? ESCAPE '\\'",
				"contract": "EXISTS (SELECT 1 FROM task_contract_steps "
				"WHERE task_id = tasks.id AND text LIKE ? ESCAPE '\\')",
				"files": "EXISTS (SELECT 1 FROM task_files "
				"WHERE task_id = tasks.id AND text LIKE ? ESCAPE '\\')",
			}
			chunk_conditions = {
				"title": "chunks.title LIKE ? ESCAPE '\\'",
				"description": "chunks.description LIKE ? ESCAPE '\\'",
			}
			selected_task_fields = tuple(
				field for field in selected_fields if field in task_conditions
			)
			selected_chunk_fields = tuple(
				field for field in selected_fields if field in chunk_conditions
			)

			task_rows: list[sqlite3.Row] = []
			if selected_task_fields:
				task_where, task_parameters = _search_where_clause(
					task_conditions,
					selected_task_fields,
					project.id,
					status,
					like_pattern,
				)
				task_rows = connection.execute(
					"SELECT tasks.id, tasks.title, tasks.status, tasks.overview, "
					"tasks.verification, tasks.updated_at, tasks.position "
					"FROM tasks WHERE "
					+ task_where
					+ " ORDER BY tasks.updated_at DESC, tasks.id",
					task_parameters,
				).fetchall()

			chunk_rows: list[sqlite3.Row] = []
			if selected_chunk_fields:
				chunk_where, chunk_parameters = _search_where_clause(
					chunk_conditions,
					selected_chunk_fields,
					project.id,
					status,
					like_pattern,
				)
				chunk_rows = connection.execute(
					"SELECT chunks.id, chunks.task_id, chunks.position, chunks.title, "
					"chunks.description, chunks.status, tasks.title AS task_title, "
					"tasks.updated_at AS task_updated_at "
					"FROM chunks JOIN tasks ON tasks.id = chunks.task_id WHERE "
					+ chunk_where
					+ " ORDER BY tasks.updated_at DESC, chunks.position, chunks.id",
					chunk_parameters,
				).fetchall()

			candidate_rows = [("task", row) for row in task_rows]
			candidate_rows.extend(("chunk", row) for row in chunk_rows)
			page_rows = candidate_rows[offset : offset + limit]
			items = [
				_search_task_item(connection, row, selected_task_fields, term)
				if record_type == "task"
				else _search_chunk_item(row, selected_chunk_fields, term)
				for record_type, row in page_rows
			]

			return page_response(items, limit, offset, len(candidate_rows))

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
		*,
		release_id: str | None = None,
	) -> dict[str, object]:
		"""List discovery notes, optionally for one task or release."""
		return self.note_list(
			"discovery", task_id, limit, offset, path, release_id=release_id
		)

	def decision_list(
		self,
		task_id: str | None = None,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
		*,
		release_id: str | None = None,
	) -> dict[str, object]:
		"""List decision notes, optionally for one task or release."""
		return self.note_list(
			"decision", task_id, limit, offset, path, release_id=release_id
		)

	def note_list(
		self,
		note_type: str,
		task_id: str | None = None,
		limit: int = DEFAULT_LIMIT,
		offset: int = 0,
		path: str | Path | None = None,
		*,
		release_id: str | None = None,
	) -> dict[str, object]:
		"""List one type of note in creation order, optionally for one task or one release; passing both filters is an error."""
		if note_type not in NOTE_TYPES:
			raise ValueError(f"unknown note type {note_type!r}")
		if task_id is not None and release_id is not None:
			raise ValueError("note list accepts at most one of task_id or release_id")
		if task_id is not None:
			validate_object_id(task_id, TASK_PREFIX)
		if release_id is not None:
			validate_object_id(release_id, RELEASE_PREFIX)

		limit, offset = validate_page(limit, offset)
		project = self.current_project(path)
		where = "project_id = ? AND type = ?"
		parameters: tuple[object, ...] = (project.id, note_type)
		if task_id is not None:
			where += " AND task_id = ?"
			parameters += (task_id,)
		elif release_id is not None:
			where += " AND release_id = ?"
			parameters += (release_id,)

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
		task: Task | None,
		chunk: Chunk | None,
		empty_hint: str,
		unfinished_dependency_id: str | None,
	) -> str | None:
		"""Suggest the next useful command for the selected task and chunk state.

		A waiting task points at unfinished_dependency_id, since task unblock
		rejects waiting tasks.
		"""
		if task is None:
			return empty_hint
		if task.status == "ready":
			return f"progress task start {task.id}"
		if task.status == "waiting":
			return f"progress task get {unfinished_dependency_id or task.id}"
		if task.status in {"blocked", "needs-decision"}:
			return f"progress task unblock {task.id}"
		if chunk is not None:
			return f"progress chunk complete {chunk.id}"
		return f"progress task complete {task.id}"
