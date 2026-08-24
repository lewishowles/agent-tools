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
from .reads import _TASK_COLUMNS, resolve_identifier, validate_identifier
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
		_require_text(overview, "release overview")
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
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Update only the overview of a current-project release."""
		validate_object_id(release_id, RELEASE_PREFIX)
		if overview is None:
			raise ProgressError(
				"release edit requires --overview",
				{"id": release_id},
			)
		if not isinstance(overview, str):
			raise ProgressError(
				"release overview must be text",
				{"id": release_id, "field": "overview"},
			)
		_require_text(overview, "release overview")

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
			if release["overview"] == overview:
				raise ProgressError(
					f"release {release_id} overview is already unchanged",
					{"id": release_id},
				)

			connection.execute(
				"UPDATE releases SET overview = ? WHERE id = ?",
				(overview, release_id),
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
		overview: str,
		purpose: str = "",
		contract: str = "",
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
		_require_text(overview, "task overview")
		_require_text(purpose, "task purpose")
		_require_text(contract, "task contract")
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
							files, acceptance_criteria, verification, risks, status,
							status_reason, position, created_at, started_at, completed_at, updated_at
						) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

	def task_clean(
		self,
		force: bool = False,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Remove done tasks that are safe to clear, or the current blocked set with force."""
		project = self.current_project(path)

		with self.database.transaction() as connection:
			candidates = []
			for task in connection.execute(
				"""
				SELECT id, release_id, title
				FROM tasks
				WHERE project_id = ? AND status = 'done'
				ORDER BY position, id
				""",
				(project.id,),
			).fetchall():
				notes = _task_notes(connection, task["id"], project.id)
				dependencies = _task_dependency_edges(connection, task["id"])
				candidates.append(
					{
						"task": task,
						"notes": notes,
						"dependencies": dependencies,
					}
				)

			blocking_candidates = []
			clean_candidates = []
			for candidate in candidates:
				if candidate["notes"] or candidate["dependencies"]:
					blocking_candidates.append(candidate)
				else:
					clean_candidates.append(candidate)

			blocked = [
				_clean_blocked_task(candidate) for candidate in blocking_candidates
			]
			targets = blocking_candidates if force else clean_candidates
			task_ids = [candidate["task"]["id"] for candidate in targets]

			if force:
				for task_id in task_ids:
					connection.execute(
						"DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_task_id = ?",
						(task_id, task_id),
					)

				_delete_task_notes(
					connection,
					[note for candidate in targets for note in candidate["notes"]],
				)

			for task_id in task_ids:
				connection.execute("DELETE FROM chunks WHERE task_id = ?", (task_id,))

			removed = [
				{"id": candidate["task"]["id"], "title": candidate["task"]["title"]}
				for candidate in targets
			]
			release_ids = sorted(
				{
					candidate["task"]["release_id"]
					for candidate in targets
					if candidate["task"]["release_id"] is not None
				}
			)

			for task_id in task_ids:
				connection.execute(
					"DELETE FROM tasks WHERE id = ? AND project_id = ?",
					(task_id, project.id),
				)

			releases_removed = []
			for release_id in release_ids:
				release = connection.execute(
					"SELECT id, title FROM releases WHERE id = ? AND project_id = ?",
					(release_id, project.id),
				).fetchone()
				remaining_task = connection.execute(
					"SELECT 1 FROM tasks WHERE release_id = ? LIMIT 1",
					(release_id,),
				).fetchone()
				if release is None or remaining_task is not None:
					continue

				connection.execute("DELETE FROM releases WHERE id = ?", (release_id,))
				releases_removed.append(
					{"id": release["id"], "title": release["title"]}
				)

		return {
			"removed_count": len(removed),
			"removed": removed,
			"blocked": [] if force else blocked,
			"releases_removed": releases_removed,
		}

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

	def task_edit(
		self,
		task_id: str,
		overview: str | None = None,
		purpose: str | None = None,
		contract: str | None = None,
		files: str | None = None,
		acceptance_criteria: str | None = None,
		verification: str | None = None,
		risks: str | None = None,
		clear_files: bool = False,
		clear_acceptance_criteria: bool = False,
		clear_verification: bool = False,
		clear_risks: bool = False,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Update selected planning fields without changing task lifecycle data."""
		validate_object_id(task_id, TASK_PREFIX)

		values = {
			"overview": overview,
			"purpose": purpose,
			"contract": contract,
			"files": files,
			"acceptance_criteria": acceptance_criteria,
			"verification": verification,
			"risks": risks,
		}
		clear_fields = {
			"files": clear_files,
			"acceptance_criteria": clear_acceptance_criteria,
			"verification": clear_verification,
			"risks": clear_risks,
		}

		if not any(
			value is not None or clear_fields.get(field, False)
			for field, value in values.items()
		):
			raise ProgressError(
				"task edit requires at least one field",
				{"id": task_id},
			)

		required_text_labels = {
			"overview": "task overview",
			"purpose": "task purpose",
			"contract": "task contract",
		}
		for field, value in values.items():
			if clear_fields.get(field, False) and value is not None:
				raise ProgressError(
					f"task edit accepts either --{field.replace('_', '-')} or "
					f"--clear-{field.replace('_', '-')}, not both",
					{"id": task_id, "field": field},
				)
			if value is not None and not isinstance(value, str):
				raise ProgressError(
					f"task {field} must be text",
					{"id": task_id, "field": field},
				)
			if value is not None and field in required_text_labels:
				_require_text(value, required_text_labels[field])

		project = self.current_project(path)
		nullable_fields = {"files"}
		updates = []
		parameters: list[object] = []
		for field, value in values.items():
			if value is None and not clear_fields.get(field, False):
				continue

			if clear_fields.get(field, False):
				value = None if field in nullable_fields else ""

			updates.append(f"{field} = ?")
			parameters.append(value)

		with self.database.transaction() as connection:
			if _task_row(connection, task_id, project.id) is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			connection.execute(
				f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
				(*parameters, task_id),
			)
			return _task_dict(connection, task_id, project.id)

	def task_move(
		self,
		task_id: str,
		before_task_id: str | None = None,
		after_task_id: str | None = None,
		path: str | Path | None = None,
		release_id: str | None = None,
	) -> dict[str, object]:
		"""Move a task within its queue or reassign it to another queue."""
		validate_object_id(task_id, TASK_PREFIX)
		release_reassignment = release_id is not None
		if release_reassignment:
			release_id = release_id or None
			if release_id is not None:
				validate_object_id(release_id, RELEASE_PREFIX)
			if before_task_id is not None and after_task_id is not None:
				raise InvalidTransitionError(
					"task move accepts at most one of before_task_id or after_task_id",
					{"task_id": task_id},
				)
		else:
			if (before_task_id is None) == (after_task_id is None):
				raise InvalidTransitionError(
					"task move requires exactly one of before_task_id or after_task_id",
					{"task_id": task_id},
				)

		target_task_id = before_task_id or after_task_id
		if target_task_id is not None:
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

			if release_reassignment:
				if release_id is not None:
					release = connection.execute(
						"SELECT 1 FROM releases WHERE id = ? AND project_id = ?",
						(release_id, project.id),
					).fetchone()
					if release is None:
						raise NotFoundError(
							f"release {release_id} was not found", {"id": release_id}
						)

				if target_task_id is not None:
					target = _task_row(connection, target_task_id, project.id)
					if target is None:
						raise NotFoundError(
							f"task {target_task_id} was not found",
							{"id": target_task_id},
						)
					if target["release_id"] != release_id:
						raise InvalidTransitionError(
							"target task must belong to the target release or unassigned queue",
							{"task_id": task_id, "target_task_id": target_task_id},
						)

				task_position = _next_task_position(connection, project.id, release_id)
				connection.execute(
					"UPDATE tasks SET release_id = ?, position = ? WHERE id = ?",
					(release_id, task_position, task_id),
				)

				if target_task_id is not None:
					_reorder_task_queue(
						connection,
						project.id,
						release_id,
						task_id,
						target_task_id,
						before_task_id,
						after_task_id,
					)

				return _task_dict(connection, task_id, project.id)

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

			_reorder_task_queue(
				connection,
				project.id,
				task["release_id"],
				task_id,
				target_task_id,
				before_task_id,
				after_task_id,
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
		description: str,
		position: int | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Create a pending chunk at the next position for a current-project task."""
		validate_object_id(task_id, TASK_PREFIX)
		_require_text(title, "chunk title")
		_require_text(description, "chunk description")
		project = self.current_project(path)

		with self.database.transaction() as connection:
			if _task_row(connection, task_id, project.id) is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			chunk_id = _new_id(connection, CHUNK_PREFIX, "chunks")
			chunk_position = (
				position
				if position is not None
				else _next_chunk_position(connection, task_id)
			)
			connection.execute(
				"""
				INSERT INTO chunks (id, task_id, position, title, description, status, started_at, completed_at)
				VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL)
				""",
				(chunk_id, task_id, chunk_position, title, description),
			)

			return _chunk_dict(connection, chunk_id, project.id)

	def chunk_move(
		self,
		chunk_id: str,
		before_chunk_id: str | None = None,
		after_chunk_id: str | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Move a chunk before or after another chunk on the same task."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		if (before_chunk_id is None) == (after_chunk_id is None):
			raise InvalidTransitionError(
				"chunk move requires exactly one of before_chunk_id or after_chunk_id",
				{"chunk_id": chunk_id},
			)

		target_chunk_id = before_chunk_id or after_chunk_id
		validate_object_id(target_chunk_id, CHUNK_PREFIX)
		if target_chunk_id == chunk_id:
			raise InvalidTransitionError(
				"a chunk cannot move relative to itself", {"chunk_id": chunk_id}
			)

		project = self.current_project(path)
		with self.database.transaction() as connection:
			chunk = _chunk_row(connection, chunk_id, project.id)
			if chunk is None:
				raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})

			target = _chunk_row(connection, target_chunk_id, project.id)
			if target is None:
				raise NotFoundError(
					f"chunk {target_chunk_id} was not found", {"id": target_chunk_id}
				)

			if chunk["task_id"] != target["task_id"]:
				raise InvalidTransitionError(
					"chunks must belong to the same task",
					{"chunk_id": chunk_id, "target_chunk_id": target_chunk_id},
				)

			chunk_rows = connection.execute(
				"SELECT id, position FROM chunks WHERE task_id = ? ORDER BY position, id",
				(chunk["task_id"],),
			).fetchall()
			ordered_ids = [row["id"] for row in chunk_rows]
			positions = {row["id"]: row["position"] for row in chunk_rows}
			ordered_ids.remove(chunk_id)
			target_index = ordered_ids.index(target_chunk_id)
			insert_index = (
				target_index if before_chunk_id is not None else target_index + 1
			)
			ordered_ids.insert(insert_index, chunk_id)

			first_position = min(positions.values(), default=1)
			for index, ordered_id in enumerate(ordered_ids, start=first_position):
				if positions[ordered_id] == index:
					continue

				connection.execute(
					"UPDATE chunks SET position = ? WHERE id = ?",
					(index, ordered_id),
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

	def chunk_edit(
		self,
		chunk_id: str,
		description: str | None = None,
		path: str | Path | None = None,
	) -> dict[str, object]:
		"""Update a chunk description without changing chunk lifecycle data."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		if description is None:
			raise ProgressError(
				"chunk edit requires --description",
				{"id": chunk_id},
			)
		if not isinstance(description, str):
			raise ProgressError(
				"chunk description must be text",
				{"id": chunk_id, "field": "description"},
			)
		_require_text(description, "chunk description")

		project = self.current_project(path)

		with self.database.transaction() as connection:
			chunk = _chunk_row(connection, chunk_id, project.id)
			if chunk is None:
				raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})
			connection.execute(
				"UPDATE chunks SET description = ? WHERE id = ?",
				(description, chunk_id),
			)
			return _chunk_dict(connection, chunk_id, project.id)

	def task_start(
		self, task_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Start a ready task by ID or project slug, activate its first pending chunk, and demote any other in-progress task to ready."""
		task_id = validate_identifier(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			task_id = resolve_identifier(connection, task_id, TASK_PREFIX, project.id)
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
		"""Complete a ready or in-progress task by ID or project slug after its chunks finish, and unblock eligible dependents."""
		task_id = validate_identifier(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			task_id = resolve_identifier(connection, task_id, TASK_PREFIX, project.id)
			task = _task_row(connection, task_id, project.id)
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
			if task["status"] not in {"ready", "in-progress"}:
				raise InvalidTransitionError(
					f"task {task_id} must be ready or in progress before it can complete",
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

			unblocked_tasks = []
			dependent_rows = connection.execute(
				"""
				SELECT tasks.id, tasks.slug, tasks.title
				FROM tasks
				JOIN task_dependencies
					ON task_dependencies.task_id = tasks.id
				WHERE tasks.project_id = ?
					AND task_dependencies.depends_on_task_id = ?
					AND tasks.status = 'blocked'
				ORDER BY tasks.position, tasks.id
				""",
				(project.id, task_id),
			).fetchall()
			for dependent in dependent_rows:
				if _unresolved_dependencies(connection, dependent["id"]):
					continue

				_unblock_task(connection, dependent["id"], project.id)
				unblocked_tasks.append(
					{
						"id": dependent["id"],
						"slug": dependent["slug"],
						"title": dependent["title"],
					}
				)

			completed_task = _task_dict(connection, task_id, project.id)
			completed_task["unblocked_tasks"] = unblocked_tasks
			return completed_task

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
		"""Complete a pending or active chunk and advance only from active."""
		validate_object_id(chunk_id, CHUNK_PREFIX)
		project = self.current_project(path)

		with self.database.transaction() as connection:
			chunk = _chunk_row(connection, chunk_id, project.id)
			if chunk is None:
				raise NotFoundError(f"chunk {chunk_id} was not found", {"id": chunk_id})
			if chunk["status"] not in {"pending", "active"}:
				raise InvalidTransitionError(
					f"chunk {chunk_id} must be pending or active before it can complete",
					{"id": chunk_id, "status": chunk["status"]},
				)

			previous_status = chunk["status"]
			now = utc_timestamp()
			connection.execute(
				"UPDATE chunks SET status = 'done', completed_at = ? WHERE id = ?",
				(now, chunk_id),
			)
			if previous_status == "active":
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


def _task_notes(
	connection: sqlite3.Connection,
	task_id: str,
	project_id: str,
) -> list[sqlite3.Row]:
	"""Return one task's notes in creation order for clean inspection."""
	return connection.execute(
		f"""
		SELECT {_NOTE_COLUMNS}
		FROM notes
		WHERE project_id = ? AND task_id = ?
		ORDER BY created_at, id
		""",
		(project_id, task_id),
	).fetchall()


def _task_dependency_edges(
	connection: sqlite3.Connection,
	task_id: str,
) -> list[dict[str, str]]:
	"""Return every dependency edge where a task is either endpoint."""
	return [
		{
			"task_id": row["task_id"],
			"depends_on_task_id": row["depends_on_task_id"],
			"other_task_id": (
				row["depends_on_task_id"]
				if row["task_id"] == task_id
				else row["task_id"]
			),
			"other_task_title": (
				row["depends_on_task_title"]
				if row["task_id"] == task_id
				else row["task_title"]
			),
			"direction": ("depends_on" if row["task_id"] == task_id else "required_by"),
		}
		for row in connection.execute(
			"""
			SELECT
				task_dependencies.task_id,
				task_dependencies.depends_on_task_id,
				dependent_task.title AS task_title,
				dependency_task.title AS depends_on_task_title
			FROM task_dependencies
			JOIN tasks AS dependent_task
				ON dependent_task.id = task_dependencies.task_id
			JOIN tasks AS dependency_task
				ON dependency_task.id = task_dependencies.depends_on_task_id
			WHERE task_dependencies.task_id = ?
				OR task_dependencies.depends_on_task_id = ?
			ORDER BY task_dependencies.task_id, task_dependencies.depends_on_task_id
			""",
			(task_id, task_id),
		).fetchall()
	]


def _clean_blocked_task(candidate: dict[str, object]) -> dict[str, object]:
	"""Build the public blocked-task record before any forced deletion."""
	task = candidate["task"]
	return {
		"id": task["id"],
		"title": task["title"],
		"notes": [
			{"type": note["type"], "body": note["body"]} for note in candidate["notes"]
		],
		"dependencies": candidate["dependencies"],
	}


def _delete_task_notes(
	connection: sqlite3.Connection, notes: list[sqlite3.Row]
) -> None:
	"""Delete selected notes from leaves upward without breaking supersession links."""
	pending = {note["id"]: note for note in notes}
	while pending:
		pending_ids = tuple(pending)
		placeholders = ", ".join("?" for _ in pending_ids)
		superseding_rows = connection.execute(
			f"SELECT id, supersedes_id FROM notes WHERE supersedes_id IN ({placeholders})",
			pending_ids,
		).fetchall()
		superseded_ids = {row["supersedes_id"] for row in superseding_rows}
		leaves = [note for note in pending.values() if note["id"] not in superseded_ids]
		if not leaves:
			_raise_if_referenced(
				"note",
				next(iter(pending)),
				{"notes": [row["id"] for row in superseding_rows]},
			)

		for note_type in ("discovery", "decision"):
			for note in leaves:
				if note["type"] != note_type:
					continue

				connection.execute("DELETE FROM notes WHERE id = ?", (note["id"],))
				pending.pop(note["id"])


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
	"""Return the next position within one release list."""
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


def _reorder_task_queue(
	connection: sqlite3.Connection,
	project_id: str,
	release_id: str | None,
	task_id: str,
	target_task_id: str,
	before_task_id: str | None,
	after_task_id: str | None,
) -> None:
	"""Place one task before or after another task in the selected queue."""
	if release_id is None:
		queue_filter = "release_id IS NULL"
		queue_parameters = (project_id,)
	else:
		queue_filter = "release_id = ?"
		queue_parameters = (project_id, release_id)

	queue_rows = connection.execute(
		f"SELECT id, position FROM tasks WHERE project_id = ? AND {queue_filter} "
		"ORDER BY position, id",
		queue_parameters,
	).fetchall()
	ordered_ids = [row["id"] for row in queue_rows]
	positions = {row["id"]: row["position"] for row in queue_rows}
	ordered_ids.remove(task_id)
	target_index = ordered_ids.index(target_task_id)
	insert_index = target_index if before_task_id is not None else target_index + 1
	ordered_ids.insert(insert_index, task_id)

	first_position = min(positions.values(), default=1)
	for index, ordered_id in enumerate(ordered_ids, start=first_position):
		if positions[ordered_id] == index:
			continue

		connection.execute(
			"UPDATE tasks SET position = ? WHERE id = ?",
			(index, ordered_id),
		)


def _next_chunk_position(connection: sqlite3.Connection, task_id: str) -> int:
	"""Return the first unused positive position in one task's chunk list."""
	rows = connection.execute(
		"SELECT position FROM chunks WHERE task_id = ?", (task_id,)
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
