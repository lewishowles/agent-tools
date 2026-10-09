"""Create and inspect Git worktrees owned by progress tasks."""

import sqlite3
from pathlib import Path

from .errors import (
	AlreadyExistsError,
	GitBindingError,
	InvalidTransitionError,
	NotFoundError,
	ProgressError,
	UnresolvedDependenciesError,
)
from .ids import TASK_PREFIX, validate_object_id
from .projects import _StoreBase
from .repository import GitRepository
from .schema import utc_timestamp
from .writes import _unresolved_dependencies


class WorktreeStore(_StoreBase):
	"""Keep a task's recorded checkout tied to its project and Git repository."""

	def cleanup(self, task_id: str, path: str | Path | None = None) -> dict[str, str]:
		"""Remove a done task's managed checkout when Git finds it clean.

		Return status "removed", "pending" with a reason, or "none" when the task
		has no recorded checkout. A checkout with edits, a locked checkout, or one
		that contains the current folder or the given path stays on disk with its
		record kept, so cleanup can be retried later. A recorded folder that no longer
		exists has its record cleared. The task's branch is never deleted. Raise
		when the task is missing or not done.
		"""
		task_id = validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)

		with self.database.connection() as connection:
			task = connection.execute(
				"SELECT status FROM tasks WHERE id = ? AND project_id = ?",
				(task_id, project.id),
			).fetchone()
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
			if task["status"] != "done":
				raise InvalidTransitionError(
					f"task {task_id} must be done before its worktree can be cleaned up",
					{"id": task_id, "status": task["status"]},
				)

			row = connection.execute(
				"SELECT task_id, project_id, path, branch, common_dir "
				"FROM task_worktrees WHERE task_id = ?",
				(task_id,),
			).fetchone()

		if row is None:
			return {"status": "none"}

		try:
			repository = GitRepository(path)
			common_dir = repository.common_dir()
			target, _ = self._record_location(row, project.id, common_dir)

			if not target.exists() and not target.is_symlink():
				with self.database.transaction() as connection:
					connection.execute(
						"DELETE FROM task_worktrees WHERE task_id = ?", (task_id,)
					)
				return {"status": "removed", "reason": "folder already gone"}

			self._validated_record(row, project.id, common_dir)

			resolved_target = target.resolve()
			if Path.cwd().resolve().is_relative_to(
				resolved_target
			) or repository.path.is_relative_to(resolved_target):
				return {
					"status": "pending",
					"reason": "command uses the task worktree",
				}

			result = repository.run(["worktree", "remove", str(target)])

			if result.returncode != 0:
				return {
					"status": "pending",
					"reason": result.stderr.strip()
					or "Git could not remove the worktree",
				}

			with self.database.transaction() as connection:
				connection.execute(
					"DELETE FROM task_worktrees WHERE task_id = ?", (task_id,)
				)
			return {"status": "removed"}
		except (ProgressError, sqlite3.Error, OSError) as error:
			reason = error.message if isinstance(error, ProgressError) else str(error)
			return {"status": "pending", "reason": reason or type(error).__name__}

	def inspect_cleanup(
		self, task_id: str, path: str | Path | None = None, *, force: bool = False
	) -> dict[str, str]:
		"""Decide whether a task's checkout can be removed, without changing anything.

		Return status "none" when no checkout is recorded, "gone" when the folder no
		longer exists, "removable", or "kept" with a plain reason. With force, a
		record outside the managed location, or one that fails validation while its
		folder exists, is "unsafe": the caller deletes the record but never the
		folder. Without force, such a record, uncommitted changes, or an unfinished
		task are "kept". Every status except "none" includes the recorded path.
		"""
		task_id = validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)
		with self.database.connection() as connection:
			row = connection.execute(
				"SELECT tasks.status, task_worktrees.task_id, task_worktrees.project_id, "
				"task_worktrees.path, task_worktrees.branch, task_worktrees.common_dir "
				"FROM tasks LEFT JOIN task_worktrees ON task_worktrees.task_id = tasks.id "
				"WHERE tasks.id = ? AND tasks.project_id = ?",
				(task_id, project.id),
			).fetchone()
		if row is None:
			raise NotFoundError(f"task {task_id} was not found", {"id": task_id})
		if row["path"] is None:
			return {"status": "none"}

		checkout_path = row["path"]
		if (
			force
			and not Path(checkout_path).exists()
			and not Path(checkout_path).is_symlink()
		):
			return {"status": "gone", "path": checkout_path}

		if Path(checkout_path) != self._managed_location(project.id, task_id)[0]:
			if force:
				return {
					"status": "unsafe",
					"path": checkout_path,
					"reason": "outside the managed location",
				}
			return {
				"status": "kept",
				"path": checkout_path,
				"reason": "recorded path is outside the managed location",
			}
		if row["status"] != "done" and not force:
			return {
				"status": "kept",
				"path": checkout_path,
				"reason": "complete the task before removing its checkout, or pass --force to discard its uncommitted changes",
			}

		try:
			target = Path(checkout_path)
			if not target.exists() and not target.is_symlink():
				return {"status": "gone", "path": checkout_path}

			repository = GitRepository(path)
			common_dir = repository.common_dir()
			target, _ = self._record_location(row, project.id, common_dir)
			self._validated_record(row, project.id, common_dir)
		except (ProgressError, OSError) as error:
			reason = error.message if isinstance(error, ProgressError) else str(error)
			return {
				"status": "unsafe" if force else "kept",
				"path": checkout_path,
				"reason": reason,
			}

		try:
			resolved_target = target.resolve()
			if Path.cwd().resolve().is_relative_to(
				resolved_target
			) or repository.path.is_relative_to(resolved_target):
				return {
					"status": "kept",
					"path": checkout_path,
					"reason": "command uses the task worktree",
				}

			worktrees = repository.run(["worktree", "list", "--porcelain"])
			if worktrees.returncode != 0:
				return {
					"status": "kept",
					"path": checkout_path,
					"reason": worktrees.stderr.strip()
					or "Git could not inspect the worktree",
				}
			for entry in worktrees.stdout.strip().split("\n\n"):
				lines = entry.splitlines()
				if not lines or lines[0] != f"worktree {target}":
					continue
				if any(
					line == "locked" or line.startswith("locked ") for line in lines
				):
					return {
						"status": "kept",
						"path": checkout_path,
						"reason": "worktree is locked",
					}
				break
			else:
				return {
					"status": "kept",
					"path": checkout_path,
					"reason": "Git does not list the recorded worktree",
				}

			if not force:
				changes = GitRepository(target).run(
					["status", "--porcelain", "--ignore-submodules=none"]
				)
				if changes.returncode != 0:
					return {
						"status": "kept",
						"path": checkout_path,
						"reason": changes.stderr.strip()
						or "Git could not inspect the worktree",
					}
				if changes.stdout:
					return {
						"status": "kept",
						"path": checkout_path,
						"reason": "uncommitted changes; pass --force to discard them",
					}
			return {"status": "removable", "path": checkout_path}
		except (ProgressError, OSError) as error:
			reason = error.message if isinstance(error, ProgressError) else str(error)
			return {"status": "kept", "path": checkout_path, "reason": reason}

	def remove_checked(
		self, checkout_path: str, path: str | Path | None = None, *, force: bool = False
	) -> dict[str, str]:
		"""Ask Git to remove a checkout that inspect_cleanup marked removable.

		Return status "removed", or "left" with Git's reason when the folder stays on
		disk. The checkout record and the task's branch are not touched.
		"""
		arguments = ["worktree", "remove"]
		if force:
			arguments.append("--force")
		try:
			result = GitRepository(path).run([*arguments, checkout_path])
		except (ProgressError, OSError) as error:
			reason = error.message if isinstance(error, ProgressError) else str(error)
			return {"status": "left", "reason": reason}
		if result.returncode != 0:
			return {
				"status": "left",
				"reason": result.stderr.strip() or "Git could not remove the worktree",
			}
		return {"status": "removed"}

	def get(self, task_id: str, path: str | Path | None = None) -> dict[str, str]:
		"""Return the task's recorded checkout without creating one.

		Raises NotFoundError when no checkout is recorded, and refuses one that no
		longer belongs to this repository, project, or task branch.
		"""
		task_id = validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)
		repository = GitRepository(path)

		with self.database.connection() as connection:
			row = connection.execute(
				"SELECT task_id, project_id, path, branch, common_dir "
				"FROM task_worktrees WHERE task_id = ?",
				(task_id,),
			).fetchone()

		if row is None:
			raise NotFoundError(
				f"task {task_id} has no managed worktree", {"task_id": task_id}
			)

		return self._validated_record(row, project.id, repository.common_dir())

	def ensure(self, task_id: str, path: str | Path | None = None) -> dict[str, object]:
		"""Create a task checkout on a new branch at the current HEAD, or return the recorded one unchanged.

		The result's `created` flag says which happened. Refuses a task that is not
		ready or in progress, has unfinished dependencies, or whose path or branch is
		already taken.
		"""
		task_id = validate_object_id(task_id, TASK_PREFIX)
		project = self.current_project(path)
		repository = GitRepository(path)
		common_dir = repository.common_dir()
		target, branch = self._managed_location(project.id, task_id)

		# A plain connection, not a write transaction: the checks below resolve the new
		# checkout's project through their own connections, which a held write lock
		# would block.
		with self.database.connection() as connection:
			task = connection.execute(
				"SELECT status FROM tasks WHERE id = ? AND project_id = ?",
				(task_id, project.id),
			).fetchone()
			if task is None:
				raise NotFoundError(f"task {task_id} was not found", {"id": task_id})

			unresolved = _unresolved_dependencies(connection, task_id)
			if unresolved:
				dependencies = [row["id"] for row in unresolved]
				raise UnresolvedDependenciesError(
					f"task {task_id} has unfinished dependencies: {', '.join(dependencies)}",
					{"task_id": task_id, "dependencies": dependencies},
				)

			if task["status"] not in {"ready", "in-progress"}:
				raise InvalidTransitionError(
					f"task {task_id} must be ready or in progress to ensure a worktree",
					{"id": task_id, "status": task["status"]},
				)

			row = connection.execute(
				"SELECT task_id, project_id, path, branch, common_dir "
				"FROM task_worktrees WHERE task_id = ?",
				(task_id,),
			).fetchone()

			if row is not None:
				record = self._validated_record(row, project.id, common_dir)
				return {**record, "created": False}

			collision = connection.execute(
				"SELECT task_id FROM task_worktrees WHERE path = ?", (str(target),)
			).fetchone()

			if collision is not None or target.exists() or target.is_symlink():
				raise AlreadyExistsError(
					f"worktree path {target} already exists",
					{"path": str(target)},
				)

			branch_result = repository.run(
				["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
			)

			if branch_result.returncode == 0:
				raise AlreadyExistsError(
					f"worktree branch {branch} already exists", {"branch": branch}
				)

			if branch_result.returncode != 1:
				raise GitBindingError(
					f"could not check worktree branch {branch}: {branch_result.stderr.strip()}"
				)

			# Add the checkout detached and create the branch only after the checks pass,
			# so a refused checkout leaves no branch behind.
			target.parent.mkdir(parents=True, exist_ok=True)
			result = repository.run(
				["worktree", "add", "--detach", str(target), "HEAD"]
			)

			if result.returncode != 0:
				raise GitBindingError(
					f"could not create worktree at {target}: {result.stderr.strip()}",
					{"path": str(target)},
				)

			checkout = GitRepository(target)
			branch_created = False
			try:
				if checkout.root() != target or checkout.common_dir() != common_dir:
					raise AlreadyExistsError(
						f"worktree path {target} belongs to another repository",
						{"path": str(target)},
					)

				# Linked worktrees share the repository's project binding.
				if self.current_project(target).id != project.id:
					raise AlreadyExistsError(
						f"worktree at {target} does not resolve to project {project.id}",
						{"path": str(target), "project_id": project.id},
					)

				result = checkout.run(["switch", "-c", branch])

				if result.returncode != 0:
					raise GitBindingError(
						f"could not create worktree branch {branch}: {result.stderr.strip()}",
						{"path": str(target), "branch": branch},
					)
				branch_created = True

				connection.execute(
					"INSERT INTO task_worktrees "
					"(task_id, project_id, path, branch, common_dir, created_at) "
					"VALUES (?, ?, ?, ?, ?, ?)",
					(
						task_id,
						project.id,
						str(target),
						branch,
						str(common_dir),
						utc_timestamp(),
					),
				)
			except Exception as error:
				# A failed setup must not leave an unrecorded checkout or branch behind.
				# The branch is checked out in the worktree, so it can be deleted only
				# after the worktree is removed, and the first failure stops the rest.
				cleanup_steps = [
					(["worktree", "remove", str(target)], {"path": str(target)})
				]

				if branch_created:
					cleanup_steps.append(
						(
							["branch", "-d", branch],
							{"path": str(target), "branch": branch},
						)
					)

				for command, details in cleanup_steps:
					cleanup = repository.run(command)

					if cleanup.returncode == 0:
						continue

					cleanup_error = cleanup.stderr.strip()
					cleanup_message = f"git {' '.join(command)} failed: {cleanup_error}"
					error.add_note(cleanup_message)

					if isinstance(error, ProgressError):
						error.message = f"{error.message}; {cleanup_message}"
						error.details.update(
							{**details, "cleanup_error": cleanup_error}
						)

					break

				raise

			return {
				"task_id": task_id,
				"project_id": project.id,
				"path": str(target),
				"branch": branch,
				"created": True,
			}

	def _managed_location(self, project_id: str, task_id: str) -> tuple[Path, str]:
		"""Return the checkout path and branch derived from a project and task."""
		# Worktrees sit beside the database, so a temporary database keeps its own.
		path = (
			self.database.path.expanduser().resolve().parent
			/ "worktrees"
			/ project_id
			/ task_id
		)
		# The branch and path come from the task, so the user never has to name them.
		branch = f"progress/task/{task_id}"
		return path, branch

	def _validated_record(
		self, row: sqlite3.Row, project_id: str, common_dir: Path
	) -> dict[str, str]:
		"""Return the saved task, project, path, and branch if they still match.

		Refuse a record whose project, path, branch, or repository no longer match.
		"""
		task_id = row["task_id"]
		path, branch = self._record_location(row, project_id, common_dir)

		checkout = GitRepository(path)

		if checkout.root() != path or checkout.common_dir() != common_dir:
			raise AlreadyExistsError(
				f"recorded worktree at {path} belongs to another repository",
				{"task_id": task_id, "path": str(path)},
			)

		result = checkout.run(["symbolic-ref", "--quiet", "--short", "HEAD"])

		if result.returncode != 0 or result.stdout.strip() != branch:
			raise AlreadyExistsError(
				f"recorded worktree at {path} is not on branch {branch}",
				{"task_id": task_id, "path": str(path), "branch": branch},
			)

		if self.current_project(path).id != project_id:
			raise AlreadyExistsError(
				f"recorded worktree at {path} belongs to another project",
				{"task_id": task_id, "path": str(path)},
			)

		return {
			"task_id": task_id,
			"project_id": project_id,
			"path": str(path),
			"branch": branch,
		}

	def _record_location(
		self, row: sqlite3.Row, project_id: str, common_dir: Path
	) -> tuple[Path, str]:
		"""Return the saved checkout path and branch for this project and repository.

		Raise when the saved record names another project, repository, path, or
		branch. The checkout folder itself is not inspected.
		"""
		task_id = row["task_id"]
		expected_path, branch = self._managed_location(project_id, task_id)
		path = Path(row["path"])

		if (
			row["project_id"] != project_id
			or row["common_dir"] != str(common_dir)
			or path != expected_path
			or row["branch"] != branch
		):
			raise AlreadyExistsError(
				f"recorded worktree for task {task_id} belongs to another checkout or project",
				{"task_id": task_id, "path": str(path)},
			)

		return path, branch
