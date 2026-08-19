"""Transactional creation and lifecycle writes for progress storage."""

from collections.abc import Iterable
from pathlib import Path
import sqlite3

from .errors import (
	AlreadyExistsError,
	DuplicateDependencyError,
	EmptyValueError,
	InvalidDependencyError,
	InvalidStatusError,
	InvalidTransitionError,
	NotFoundError,
	PendingChunksError,
	ProgressError,
	StillReferencedError,
	UnresolvedDependenciesError,
)
from .ids import (
	CHUNK_PREFIX,
	NOTE_PREFIX,
	RELEASE_PREFIX,
	TASK_PREFIX,
	generate_object_id,
	validate_object_id,
)
from .models import Chunk, Context, Note, Release, Task
from .projects import _StoreBase
from .reads import _TASK_COLUMNS
from .schema import utc_timestamp

# Status values accepted when creating or updating a release.
_RELEASE_STATUSES = frozenset({"planned", "active", "done"})
# Qualified chunk columns used by chunk write queries scoped to a task.
_QUALIFIED_CHUNK_COLUMNS = (
	"chunks.id, chunks.task_id, chunks.position, chunks.title, chunks.description, "
	"chunks.status, chunks.started_at, chunks.completed_at"
)

# Note columns returned by discovery and decision writes.
_NOTE_COLUMNS = "id, project_id, task_id, type, body, supersedes_id, created_at"
# Context columns returned by context replacement writes.
_CONTEXT_COLUMNS = (
	"project_id, current_goal, previous_step, next_step, standing_context, "
	"verify_with, stop_marker, updated_at"
)


class WriteStore(_StoreBase):
	"""Run current-project creation and lifecycle writes in short transactions."""

	def release_add(
		self,
		slug: str,
		title: str,
		overview: str = "",
		status: str = "planned",
		position: int | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Create a release at the next position in the current project."""
		_require_text(slug, "release slug")
		_require_text(title, "release title")
		if status not in _RELEASE_STATUSES:
			raise InvalidStatusError(
				f"unknown release status {status!r}",
				{"status": status, "valid_statuses": sorted(_RELEASE_STATUSES)},
			)

		project = self.current_project(path)
		with self.database.transaction() as connection:
			release_id = _new_id(connection, RELEASE_PREFIX, "releases")
			release_position = (
				position
				if position is not None
				else _next_position(connection, "releases", "project_id", project.id)
			)
			try:
				connection.execute(
					"""
					INSERT INTO releases (id, project_id, slug, title, overview, status, position)
					VALUES (?, ?, ?, ?, ?, ?, ?)
					""",
					(
						release_id,
						project.id,
						slug,
						title,
						overview,
						status,
						release_position,
					),
				)
			except sqlite3.IntegrityError as error:
				raise AlreadyExistsError(
					f"release slug {slug!r} already exists in project {project.id}",
					{"slug": slug, "project_id": project.id},
				) from error

			row = connection.execute(
				"SELECT id, project_id, slug, title, overview, status, position "
				"FROM releases WHERE id = ?",
				(release_id,),
			).fetchone()

		return Release.from_row(row).to_dict()

	def release_remove(
		self, release_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Remove a current-project release when no task still uses it."""
		validate_object_id(release_id, RELEASE_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			release = connection.execute(
				"SELECT 1 FROM releases WHERE id = ? AND project_id = ?",
				(release_id, project.id),
			).fetchone()

			if release is None:
				raise NotFoundError(
					f"release {release_id} was not found", {"id": release_id}
				)

			task_ids = [
				row["id"]
				for row in connection.execute(
					"SELECT id FROM tasks WHERE release_id = ? ORDER BY id",
					(release_id,),
				).fetchall()
			]

			_raise_if_referenced(
				"release", release_id, {"tasks": task_ids} if task_ids else {}
			)

			connection.execute("DELETE FROM releases WHERE id = ?", (release_id,))

		return {"id": release_id}

	def release_rename(
		self,
		release_id: str,
		title: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Update only the title of a current-project release."""
		validate_object_id(release_id, RELEASE_PREFIX)
		_require_text(title, "release title")
		project = self.current_project(path)

		with self.database.transaction() as connection:
			release = connection.execute(
				"SELECT 1 FROM releases WHERE id = ? AND project_id = ?",
				(release_id, project.id),
			).fetchone()
			if release is None:
				raise NotFoundError(
					f"release {release_id} was not found", {"id": release_id}
				)

			connection.execute(
				"UPDATE releases SET title = ? WHERE id = ?", (title, release_id)
			)
			return Release.from_row(
				connection.execute(
					"SELECT id, project_id, slug, title, overview, status, position "
					"FROM releases WHERE id = ?",
					(release_id,),
				).fetchone()
			).to_dict()

	def release_complete(
		self, release_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Complete a planned or active current-project release."""
		validate_object_id(release_id, RELEASE_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			release = connection.execute(
				"SELECT id, project_id, slug, title, overview, status, position "
				"FROM releases WHERE id = ? AND project_id = ?",
				(release_id, project.id),
			).fetchone()
			if release is None:
				raise NotFoundError(
					f"release {release_id} was not found", {"id": release_id}
				)
			if release["status"] not in {"planned", "active"}:
				raise InvalidTransitionError(
					f"release {release_id} cannot be completed from status {release['status']}",
					{"id": release_id, "status": release["status"]},
				)

			connection.execute(
				"UPDATE releases SET status = 'done' WHERE id = ?", (release_id,)
			)
			return Release.from_row(
				connection.execute(
					"SELECT id, project_id, slug, title, overview, status, position "
					"FROM releases WHERE id = ?",
					(release_id,),
				).fetchone()
			).to_dict()

	def release_edit(
		self,
		release_id: str,
		overview: str | None = None,
		clear_overview: bool = False,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Update only the overview of a current-project release."""
		validate_object_id(release_id, RELEASE_PREFIX)
		if overview is None and not clear_overview:
			raise ProgressError(
				"release edit requires --overview or --clear-overview",
				{"id": release_id},
			)
		if overview is not None and clear_overview:
			raise ProgressError(
				"release edit accepts either --overview or --clear-overview, not both",
				{"id": release_id},
			)

		overview_value = overview if overview is not None else ""
		project = self.current_project(path)

		with self.database.transaction() as connection:
			release = connection.execute(
				"SELECT id, project_id, slug, title, overview, status, position "
				"FROM releases WHERE id = ? AND project_id = ?",
				(release_id, project.id),
			).fetchone()
			if release is None:
				raise NotFoundError(
					f"release {release_id} was not found", {"id": release_id}
				)
			if release["overview"] == overview_value:
				raise ProgressError(
					f"release {release_id} overview is already unchanged",
					{"id": release_id},
				)

			connection.execute(
				"UPDATE releases SET overview = ? WHERE id = ?",
				(overview_value, release_id),
			)
			return Release.from_row(
				connection.execute(
					"SELECT id, project_id, slug, title, overview, status, position "
					"FROM releases WHERE id = ?",
					(release_id,),
				).fetchone()
			).to_dict()

	def task_add(
		self,
		slug: str,
		title: str,
		overview: str = "",
		purpose: str = "",
		contract: str = "",
		model_tier: str | None = None,
		files: str | None = None,
		acceptance_criteria: str = "",
		verification: str = "",
		risks: str = "",
		release_id: str | None = None,
		depends_on: Iterable[str] = (),
		position: int | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Create a task and derive its initial status from its dependencies."""
		_require_text(slug, "task slug")
		_require_text(title, "task title")
		dependency_ids = _normalise_dependencies(depends_on)
		for dependency_id in dependency_ids:
			validate_object_id(dependency_id, TASK_PREFIX)
		if release_id is not None:
			validate_object_id(release_id, RELEASE_PREFIX)

		project = self.current_project(path)
		with self.database.transaction() as connection:
			if release_id is not None:
				release = connection.execute(
					"SELECT 1 FROM releases WHERE id = ? AND project_id = ?",
					(release_id, project.id),
				).fetchone()
				if release is None:
					raise NotFoundError(
						f"release {release_id} was not found",
						{"id": release_id},
					)

			dependency_rows = []
			for dependency_id in dependency_ids:
				row = connection.execute(
					"SELECT id, status FROM tasks WHERE id = ? AND project_id = ?",
					(dependency_id, project.id),
				).fetchone()
				if row is None:
					raise NotFoundError(
						f"dependency task {dependency_id} was not found",
						{"id": dependency_id},
					)
				dependency_rows.append(row)

			task_id = _new_id(connection, TASK_PREFIX, "tasks")
			status = (
				"ready"
				if all(row["status"] == "done" for row in dependency_rows)
				else "blocked"
			)
			status_reason = (
				None if status == "ready" else _dependency_reason(dependency_rows)
			)
			created_at = utc_timestamp()
			task_position = (
				position
				if position is not None
				else _next_task_position(connection, project.id, release_id)
			)
			try:
				connection.execute(
					"""
					INSERT INTO tasks (
						id, project_id, slug, release_id, title, overview, purpose, contract,
						model_tier, files, acceptance_criteria, verification, risks, status,
						status_reason, position, created_at, started_at, completed_at, updated_at
					) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
					""",
					(
						task_id,
						project.id,
						slug,
						release_id,
						title,
						overview,
						purpose,
						contract,
						model_tier,
						files,
						acceptance_criteria,
						verification,
						risks,
						status,
						status_reason,
						task_position,
						created_at,
						None,
						None,
						created_at,
					),
				)
			except sqlite3.IntegrityError as error:
				raise AlreadyExistsError(
					f"task slug {slug!r} already exists in project {project.id}",
					{"slug": slug, "project_id": project.id},
				) from error

			for dependency_id in dependency_ids:
				connection.execute(
					"INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
					(task_id, dependency_id),
				)

			return _task_dict(connection, task_id, project.id)

	def task_remove(
		self, task_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Remove a current-project task when no child row still uses it."""
		validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			task = _task_row(connection, task_id, project.id)

			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			references: dict[str, list[str]] = {}
			chunk_ids = [
				row["id"]
				for row in connection.execute(
					"SELECT id FROM chunks WHERE task_id = ? ORDER BY id", (task_id,)
				).fetchall()
			]

			if chunk_ids:
				references["chunks"] = chunk_ids

			dependency_edges = [
				f"{row['task_id']} -> {row['depends_on_task_id']}"
				for row in connection.execute(
					"""
					SELECT task_id, depends_on_task_id
					FROM task_dependencies
					WHERE task_id = ? OR depends_on_task_id = ?
					ORDER BY task_id, depends_on_task_id
					""",
					(task_id, task_id),
				).fetchall()
			]

			if dependency_edges:
				references["dependencies"] = dependency_edges

			note_ids = [
				row["id"]
				for row in connection.execute(
					"SELECT id FROM notes WHERE task_id = ? ORDER BY id", (task_id,)
				).fetchall()
			]

			if note_ids:
				references["notes"] = note_ids

			_raise_if_referenced("task", task_id, references)

			connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

		return {"id": task_id}

	def task_rename(
		self,
		task_id: str,
		title: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Update only the title of a current-project task."""
		validate_object_id(task_id, TASK_PREFIX)
		_require_text(title, "task title")
		project = self.current_project(path)

		with self.database.transaction() as connection:
			if _task_row(connection, task_id, project.id) is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			connection.execute(
				"UPDATE tasks SET title = ? WHERE id = ?", (title, task_id)
			)
			return _task_dict(connection, task_id, project.id)

	def task_move(
		self,
		task_id: str,
		before_task_id: str | None = None,
		after_task_id: str | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Move a task before or after another task in the same queue."""
		validate_object_id(task_id, TASK_PREFIX)
		if (before_task_id is None) == (after_task_id is None):
			raise InvalidTransitionError(
				"task move requires exactly one of before_task_id or after_task_id",
				{"task_id": task_id},
			)

		target_task_id = before_task_id or after_task_id
		validate_object_id(target_task_id, TASK_PREFIX)
		if target_task_id == task_id:
			raise InvalidTransitionError(
				"a task cannot move relative to itself", {"task_id": task_id}
			)

		project = self.current_project(path)
		with self.database.transaction() as connection:
			task = _task_row(connection, task_id, project.id)
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			target = _task_row(connection, target_task_id, project.id)
			if target is None:
				raise NotFoundError(
					f"task {target_task_id} was not found", {"id": target_task_id}
				)

			if task["release_id"] != target["release_id"]:
				raise InvalidTransitionError(
					"tasks must belong to the same release or unassigned queue",
					{"task_id": task_id, "target_task_id": target_task_id},
				)

			if task["release_id"] is None:
				queue_filter = "release_id IS NULL"
				queue_parameters = (project.id,)
			else:
				queue_filter = "release_id = ?"
				queue_parameters = (project.id, task["release_id"])

			queue_rows = connection.execute(
				f"SELECT id, position FROM tasks WHERE project_id = ? AND {queue_filter} "
				"ORDER BY position, id",
				queue_parameters,
			).fetchall()
			ordered_ids = [row["id"] for row in queue_rows]
			positions = {row["id"]: row["position"] for row in queue_rows}
			ordered_ids.remove(task_id)
			target_index = ordered_ids.index(target_task_id)
			insert_index = (
				target_index if before_task_id is not None else target_index + 1
			)
			ordered_ids.insert(insert_index, task_id)

			first_position = min(positions.values(), default=1)
			for index, ordered_id in enumerate(ordered_ids, start=first_position):
				if positions[ordered_id] == index:
					continue

				connection.execute(
					"UPDATE tasks SET position = ? WHERE id = ?",
					(index, ordered_id),
				)

			return _task_dict(connection, task_id, project.id)

	def task_dependency_add(
		self,
		task_id: str,
		depends_on_task_id: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Add one same-project dependency and update a ready task if needed."""
		validate_object_id(task_id, TASK_PREFIX)
		validate_object_id(depends_on_task_id, TASK_PREFIX)
		if task_id == depends_on_task_id:
			raise InvalidDependencyError(
				"a task cannot depend on itself", {"task_id": task_id}
			)

		project = self.current_project(path)
		with self.database.transaction() as connection:
			task = _task_row(connection, task_id, project.id)
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			dependency = _task_row(connection, depends_on_task_id, project.id)
			if dependency is None:
				raise NotFoundError(
					f"dependency task {depends_on_task_id} was not found",
					{"id": depends_on_task_id},
				)

			existing = connection.execute(
				"SELECT 1 FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
				(task_id, depends_on_task_id),
			).fetchone()
			if existing is not None:
				raise DuplicateDependencyError(
					f"task {task_id} already depends on {depends_on_task_id}",
					{"task_id": task_id, "depends_on_task_id": depends_on_task_id},
				)

			if dependency["status"] != "done" and task["status"] in {
				"in-progress",
				"done",
			}:
				raise InvalidDependencyError(
					f"cannot add unfinished dependency to {task['status']} task {task_id}",
					{"task_id": task_id, "depends_on_task_id": depends_on_task_id},
				)

			connection.execute(
				"INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
				(task_id, depends_on_task_id),
			)
			if dependency["status"] != "done" and task["status"] == "ready":
				connection.execute(
					"UPDATE tasks SET status = 'blocked', status_reason = ?, updated_at = ? WHERE id = ?",
					(_dependency_reason([dependency]), utc_timestamp(), task_id),
				)

			return _task_dict(connection, task_id, project.id)

	def task_dependency_remove(
		self,
		task_id: str,
		depends_on_task_id: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Remove one existing same-project task dependency edge."""
		validate_object_id(task_id, TASK_PREFIX)
		validate_object_id(depends_on_task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			if _task_row(connection, task_id, project.id) is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
			if _task_row(connection, depends_on_task_id, project.id) is None:
				raise NotFoundError(
					f"dependency task {depends_on_task_id} was not found",
					{"id": depends_on_task_id},
				)

			edge = connection.execute(
				"""
				SELECT task_id, depends_on_task_id
				FROM task_dependencies
				WHERE task_id = ? AND depends_on_task_id = ?
				""",
				(task_id, depends_on_task_id),
			).fetchone()

			if edge is None:
				raise NotFoundError(
					f"task {task_id} does not depend on {depends_on_task_id}",
					{
						"task_id": task_id,
						"depends_on_task_id": depends_on_task_id,
					},
				)

			connection.execute(
				"DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
				(task_id, depends_on_task_id),
			)

		return {
			"task_id": task_id,
			"depends_on_task_id": depends_on_task_id,
		}

	def chunk_add(
		self,
		task_id: str,
		title: str,
		description: str = "",
		position: int | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Create a pending chunk at the next position for a current-project task."""
		validate_object_id(task_id, TASK_PREFIX)
		_require_text(title, "chunk title")
		project = self.current_project(path)

		with self.database.transaction() as connection:
			if _task_row(connection, task_id, project.id) is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			chunk_id = _new_id(connection, CHUNK_PREFIX, "chunks")
			chunk_position = (
				position
				if position is not None
				else _next_position(connection, "chunks", "task_id", task_id)
			)
			connection.execute(
				"""
				INSERT INTO chunks (id, task_id, position, title, description, status, started_at, completed_at)
				VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL)
				""",
				(chunk_id, task_id, chunk_position, title, description),
			)

			return _chunk_dict(connection, chunk_id, project.id)

	def chunk_start(
		self, chunk_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Activate a pending chunk on an in-progress task atomically."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			chunk = _chunk_row(connection, chunk_id, project.id)
			if chunk is None:
				raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})
			if chunk["status"] != "pending":
				raise InvalidTransitionError(
					f"chunk {chunk_id} must be pending before it can start",
					{"id": chunk_id, "status": chunk["status"]},
				)

			task = _task_row(connection, chunk["task_id"], project.id)
			if task["status"] != "in-progress":
				raise InvalidTransitionError(
					f"task {task['id']} must be in progress before a chunk can start; "
					f"run progress task start {task['id']} first",
					{
						"id": task["id"],
						"status": task["status"],
						"chunk_id": chunk_id,
					},
				)

			now = utc_timestamp()
			connection.execute(
				"UPDATE chunks SET status = 'pending', started_at = NULL "
				"WHERE task_id = ? AND status = 'active'",
				(chunk["task_id"],),
			)
			connection.execute(
				"UPDATE chunks SET status = 'active', started_at = ? WHERE id = ?",
				(now, chunk_id),
			)

			return _chunk_dict(connection, chunk_id, project.id)

	def chunk_remove(
		self, chunk_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Remove a current-project chunk."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			if _chunk_row(connection, chunk_id, project.id) is None:
				raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})

			connection.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))

		return {"id": chunk_id}

	def chunk_rename(
		self,
		chunk_id: str,
		title: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Update only the title of a current-project chunk."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		_require_text(title, "chunk title")
		project = self.current_project(path)

		with self.database.transaction() as connection:
			if _chunk_row(connection, chunk_id, project.id) is None:
				raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})

			connection.execute(
				"UPDATE chunks SET title = ? WHERE id = ?", (title, chunk_id)
			)
			return _chunk_dict(connection, chunk_id, project.id)

	def task_start(
		self, task_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Start a ready task, activate its first pending chunk, and demote any other in-progress task to ready."""
		validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			task = _task_row(connection, task_id, project.id)
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
			if task["status"] != "ready":
				raise InvalidTransitionError(
					f"task {task_id} must be ready before it can start",
					{"id": task_id, "status": task["status"]},
				)

			unresolved = _unresolved_dependencies(connection, task_id)
			if unresolved:
				raise UnresolvedDependenciesError(
					f"task {task_id} has unfinished dependencies: {', '.join(row['id'] for row in unresolved)}",
					{
						"task_id": task_id,
						"dependencies": [row["id"] for row in unresolved],
					},
				)

			now = utc_timestamp()

			other_task = connection.execute(
				f"SELECT {_TASK_COLUMNS} FROM tasks "
				"WHERE project_id = ? AND status = 'in-progress' AND id != ? "
				"ORDER BY position, id LIMIT 1",
				(project.id, task_id),
			).fetchone()

			demoted_task = None
			if other_task is not None:
				connection.execute(
					"UPDATE chunks SET status = 'pending', started_at = NULL WHERE task_id = ? AND status = 'active'",
					(other_task["id"],),
				)
				connection.execute(
					"UPDATE tasks SET status = 'ready', status_reason = NULL, updated_at = ? WHERE id = ?",
					(now, other_task["id"]),
				)
				demoted_task = {
					"id": other_task["id"],
					"slug": other_task["slug"],
					"title": other_task["title"],
				}

			connection.execute(
				"""
				UPDATE tasks
				SET status = 'in-progress', status_reason = NULL, started_at = ?,
					completed_at = NULL, updated_at = ?
				WHERE id = ?
				""",
				(now, now, task_id),
			)
			pending_chunk = connection.execute(
				f"SELECT {_QUALIFIED_CHUNK_COLUMNS} FROM chunks "
				"WHERE task_id = ? AND status = 'pending' ORDER BY position, id LIMIT 1",
				(task_id,),
			).fetchone()
			if pending_chunk is not None:
				connection.execute(
					"UPDATE chunks SET status = 'active', started_at = ? WHERE id = ?",
					(now, pending_chunk["id"]),
				)

			result = _task_dict(connection, task_id, project.id)
			result["demoted_task"] = demoted_task
			return result

	def task_complete(
		self, task_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Complete an in-progress task only after every chunk is finished or skipped."""
		validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			task = _task_row(connection, task_id, project.id)
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
			if task["status"] != "in-progress":
				raise InvalidTransitionError(
					f"task {task_id} must be in progress before it can complete",
					{"id": task_id, "status": task["status"]},
				)

			pending = connection.execute(
				"SELECT id FROM chunks WHERE task_id = ? AND status IN ('pending', 'active') "
				"ORDER BY position, id",
				(task_id,),
			).fetchall()
			if pending:
				raise PendingChunksError(
					f"task {task_id} still has unfinished chunks: {', '.join(row['id'] for row in pending)}",
					{"task_id": task_id, "chunks": [row["id"] for row in pending]},
				)

			now = utc_timestamp()
			connection.execute(
				"UPDATE tasks SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ?",
				(now, now, task_id),
			)

			return _task_dict(connection, task_id, project.id)

	def task_block(
		self,
		task_id: str,
		reason: str,
		needs_decision: bool = False,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Block a ready or in-progress task, returning its active chunk to pending."""
		validate_object_id(task_id, TASK_PREFIX)
		_require_text(reason, "block reason")
		project = self.current_project(path)

		with self.database.transaction() as connection:
			task = _task_row(connection, task_id, project.id)
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
			if task["status"] not in {"ready", "in-progress"}:
				raise InvalidTransitionError(
					f"task {task_id} cannot be blocked from status {task['status']}",
					{"id": task_id, "status": task["status"]},
				)

			now = utc_timestamp()
			if task["status"] == "in-progress":
				connection.execute(
					"UPDATE chunks SET status = 'pending', started_at = NULL WHERE task_id = ? AND status = 'active'",
					(task_id,),
				)
			connection.execute(
				"UPDATE tasks SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
				(
					"needs-decision" if needs_decision else "blocked",
					reason,
					now,
					task_id,
				),
			)

			return _task_dict(connection, task_id, project.id)

	def task_unblock(
		self, task_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Make a blocked task ready when all its dependencies are done."""
		validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			_unblock_task(connection, task_id, project.id)

			return _task_dict(connection, task_id, project.id)

	def chunk_complete(
		self, chunk_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Complete the active chunk and activate the next pending chunk atomically."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			chunk = _chunk_row(connection, chunk_id, project.id)
			if chunk is None:
				raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})
			if chunk["status"] != "active":
				raise InvalidTransitionError(
					f"chunk {chunk_id} must be active before it can complete",
					{"id": chunk_id, "status": chunk["status"]},
				)

			now = utc_timestamp()
			connection.execute(
				"UPDATE chunks SET status = 'done', completed_at = ? WHERE id = ?",
				(now, chunk_id),
			)
			next_chunk = connection.execute(
				f"SELECT {_QUALIFIED_CHUNK_COLUMNS} FROM chunks "
				"WHERE task_id = ? AND status = 'pending' ORDER BY position, id LIMIT 1",
				(chunk["task_id"],),
			).fetchone()
			if next_chunk is not None:
				connection.execute(
					"UPDATE chunks SET status = 'active', started_at = ? WHERE id = ?",
					(now, next_chunk["id"]),
				)

			return _chunk_dict(connection, chunk_id, project.id)

	def discovery_add(
		self,
		task_id: str,
		body: str,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Store a discovery note for a current-project task."""
		return self._note_add("discovery", task_id, body, None, path)

	def decision_add(
		self,
		task_id: str,
		body: str,
		supersedes_id: str | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Store a decision note, optionally superseding an earlier note."""
		return self._note_add("decision", task_id, body, supersedes_id, path)

	def discovery_remove(
		self, note_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Remove a discovery note when no later note supersedes it."""
		return self._note_remove("discovery", note_id, path)

	def decision_remove(
		self, note_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Remove a decision note when no later note supersedes it."""
		return self._note_remove("decision", note_id, path)

	def _note_remove(
		self,
		note_type: str,
		note_id: str,
		path: str | Path | None,
	) -> dict[str, object]:
		"""Remove one typed current-project note without orphaning a superseder."""
		validate_object_id(note_id, NOTE_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			note = connection.execute(
				f"SELECT {_NOTE_COLUMNS} FROM notes WHERE id = ? AND project_id = ?",
				(note_id, project.id),
			).fetchone()

			if note is None or note["type"] != note_type:
				raise NotFoundError(
					f"{note_type} note {note_id} was not found", {"id": note_id}
				)

			superseding_ids = [
				row["id"]
				for row in connection.execute(
					"SELECT id FROM notes WHERE supersedes_id = ? ORDER BY id",
					(note_id,),
				).fetchall()
			]

			_raise_if_referenced(
				f"{note_type} note",
				note_id,
				{"notes": superseding_ids} if superseding_ids else {},
			)

			connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))

		return {"id": note_id}

	def _note_add(
		self,
		note_type: str,
		task_id: str,
		body: str,
		supersedes_id: str | None,
		path: str | Path | None,
	) -> dict[str, object]:
		"""Insert one validated note in the current project."""
		validate_object_id(task_id, TASK_PREFIX)
		_require_text(body, "note body")
		if supersedes_id is not None:
			validate_object_id(supersedes_id, NOTE_PREFIX)

		project = self.current_project(path)
		with self.database.transaction() as connection:
			if _task_row(connection, task_id, project.id) is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
			if supersedes_id is not None:
				superseded = connection.execute(
					"SELECT 1 FROM notes WHERE id = ? AND project_id = ?",
					(supersedes_id, project.id),
				).fetchone()
				if superseded is None:
					raise NotFoundError(
						f"note {supersedes_id} was not found",
						{"id": supersedes_id},
					)

			note_id = _new_id(connection, NOTE_PREFIX, "notes")
			created_at = utc_timestamp()
			connection.execute(
				"""
				INSERT INTO notes (id, project_id, task_id, type, body, supersedes_id, created_at)
				VALUES (?, ?, ?, ?, ?, ?, ?)
				""",
				(
					note_id,
					project.id,
					task_id,
					note_type,
					body,
					supersedes_id,
					created_at,
				),
			)

			return Note.from_row(
				connection.execute(
					f"SELECT {_NOTE_COLUMNS} FROM notes WHERE id = ?", (note_id,)
				).fetchone()
			).to_dict()

	def context_set(
		self,
		current_goal: str | None = None,
		previous_step: str | None = None,
		next_step: str | None = None,
		standing_context: str | None = None,
		verify_with: str | None = None,
		stop_marker: str | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Replace the current project's single handoff context row."""
		project = self.current_project(path)
		updated_at = utc_timestamp()
		with self.database.transaction() as connection:
			connection.execute(
				"""
				INSERT INTO context (
					project_id, current_goal, previous_step, next_step, standing_context,
					verify_with, stop_marker, updated_at
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
				ON CONFLICT(project_id) DO UPDATE SET
					current_goal = excluded.current_goal,
					previous_step = excluded.previous_step,
					next_step = excluded.next_step,
					standing_context = excluded.standing_context,
					verify_with = excluded.verify_with,
					stop_marker = excluded.stop_marker,
					updated_at = excluded.updated_at
				""",
				(
					project.id,
					current_goal,
					previous_step,
					next_step,
					standing_context,
					verify_with,
					stop_marker,
					updated_at,
				),
			)

			row = connection.execute(
				f"SELECT {_CONTEXT_COLUMNS} FROM context WHERE project_id = ?",
				(project.id,),
			).fetchone()

		return Context.from_row(row).to_dict()


def _unblock_task(
	connection: sqlite3.Connection, task_id: str, project_id: str
) -> None:
	"""Make one blocked task ready when all its dependencies are done."""
	task = _task_row(connection, task_id, project_id)
	if task is None:
		raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
	if task["status"] not in {"blocked", "needs-decision"}:
		raise InvalidTransitionError(
			f"task {task_id} is not blocked",
			{"id": task_id, "status": task["status"]},
		)

	unresolved = _unresolved_dependencies(connection, task_id)
	if unresolved:
		raise UnresolvedDependenciesError(
			f"task {task_id} has unfinished dependencies: {', '.join(row['id'] for row in unresolved)}",
			{
				"task_id": task_id,
				"dependencies": [row["id"] for row in unresolved],
			},
		)

	connection.execute(
		"UPDATE tasks SET status = 'ready', status_reason = NULL, updated_at = ? WHERE id = ?",
		(utc_timestamp(), task_id),
	)


def _require_text(value: str, label: str) -> None:
	"""Reject required text values that contain no non-whitespace characters."""
	if not value.strip():
		raise EmptyValueError(f"{label} must not be empty")


def _raise_if_referenced(
	object_type: str,
	object_id: str,
	references: dict[str, list[str]],
) -> None:
	"""Reject a delete while naming every row that still references the object."""
	if not references:
		return

	reference_text = "; ".join(
		f"{label}: {', '.join(values)}" for label, values in references.items()
	)

	children = [child for values in references.values() for child in values]

	raise StillReferencedError(
		f"{object_type} {object_id} is still referenced by {reference_text}",
		{"id": object_id, "references": references, "children": children},
	)


def _normalise_dependencies(depends_on: Iterable[str]) -> tuple[str, ...]:
	"""Return dependency IDs in order, rejecting duplicates rather than deduplicating them."""
	if isinstance(depends_on, str):
		depends_on = (depends_on,)

	dependency_ids = tuple(depends_on)
	if len(set(dependency_ids)) != len(dependency_ids):
		duplicates = sorted(
			dependency_id
			for dependency_id in set(dependency_ids)
			if dependency_ids.count(dependency_id) > 1
		)
		raise DuplicateDependencyError(
			f"task dependencies were repeated: {', '.join(duplicates)}",
			{"dependencies": duplicates},
		)

	return dependency_ids


def _new_id(connection: sqlite3.Connection, prefix: str, table: str) -> str:
	"""Generate a unique ID while the caller's write transaction is open."""
	return generate_object_id(
		prefix,
		lambda candidate: (
			connection.execute(
				f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)
			).fetchone()
			is not None
		),
	)


def _next_position(
	connection: sqlite3.Connection, table: str, column: str, value: str
) -> int:
	"""Return the next position within one project's release, task, or chunk list."""
	return int(
		connection.execute(
			f"SELECT COALESCE(MAX(position), 0) + 1 FROM {table} WHERE {column} = ?",
			(value,),
		).fetchone()[0]
	)


def _next_task_position(
	connection: sqlite3.Connection, project_id: str, release_id: str | None
) -> int:
	"""Return the first unused positive position in one task queue."""
	if release_id is None:
		rows = connection.execute(
			"SELECT position FROM tasks WHERE project_id = ? AND release_id IS NULL",
			(project_id,),
		).fetchall()
	else:
		rows = connection.execute(
			"SELECT position FROM tasks WHERE project_id = ? AND release_id = ?",
			(project_id, release_id),
		).fetchall()

	used_positions = {row["position"] for row in rows}
	position = 1
	while position in used_positions:
		position += 1

	return position


def _task_row(
	connection: sqlite3.Connection, task_id: str, project_id: str
) -> sqlite3.Row | None:
	"""Fetch one current-project task row."""
	return connection.execute(
		f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = ? AND project_id = ?",
		(task_id, project_id),
	).fetchone()


def _task_dict(
	connection: sqlite3.Connection, task_id: str, project_id: str
) -> dict[str, object]:
	"""Fetch one task and convert it to its stable public shape."""
	row = _task_row(connection, task_id, project_id)
	if row is None:
		raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

	return Task.from_row(row).to_dict()


def _chunk_row(
	connection: sqlite3.Connection, chunk_id: str, project_id: str
) -> sqlite3.Row | None:
	"""Fetch one chunk belonging to a current-project task."""
	return connection.execute(
		f"SELECT {_QUALIFIED_CHUNK_COLUMNS} FROM chunks "
		"JOIN tasks ON tasks.id = chunks.task_id "
		"WHERE chunks.id = ? AND tasks.project_id = ?",
		(chunk_id, project_id),
	).fetchone()


def _chunk_dict(
	connection: sqlite3.Connection, chunk_id: str, project_id: str
) -> dict[str, object]:
	"""Fetch one chunk and convert it to its stable public shape."""
	row = _chunk_row(connection, chunk_id, project_id)
	if row is None:
		raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})

	return Chunk.from_row(row).to_dict()


def _unresolved_dependencies(
	connection: sqlite3.Connection, task_id: str
) -> list[sqlite3.Row]:
	"""Return unfinished dependency rows in deterministic ID order."""
	return connection.execute(
		"""
		SELECT dependency.id, dependency.status
		FROM task_dependencies AS dependencies
		JOIN tasks AS dependency ON dependency.id = dependencies.depends_on_task_id
		WHERE dependencies.task_id = ? AND dependency.status != 'done'
		ORDER BY dependency.id
		""",
		(task_id,),
	).fetchall()


def _dependency_reason(rows: Iterable[sqlite3.Row]) -> str:
	"""Build the persisted reason for a task blocked by unfinished dependencies."""
	return "unresolved dependencies: " + ", ".join(row["id"] for row in rows)
