"""Create and inspect Git worktrees owned by progress tasks."""

import sqlite3
from pathlib import Path
from shlex import quote

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

	def cleanup(
		self, task_id: str, path: str | Path | None = None
	) -> dict[str, object]:
		"""Merge a done task's branch into its base, then remove its checkout.

		The task's commits are replayed onto the base branch recorded when the
		checkout was created, the base is fast-forwarded to them, the checkout is
		removed and the merged task branch is deleted. Return status "removed"
		with the number of commits merged, "pending" with a reason, or "none" when
		the task has no recorded checkout. Removed and pending results include
		the path, task branch and base branch.

		A checkout with uncommitted changes, an unknown or missing base, a rebase
		conflict, a locked checkout, a refused fast-forward, or a checkout that
		contains the current folder or the given path stays on disk with its branch
		and record, so cleanup can be retried later. A removal failure can be
		pending after the commits reached the base; retry then only removes the
		checkout. A recorded folder that no longer exists has its record cleared.
		Raise when the task is missing or not done.
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
				"SELECT task_id, project_id, path, branch, base_branch, common_dir "
				"FROM task_worktrees WHERE task_id = ?",
				(task_id,),
			).fetchone()

		if row is None:
			return {"status": "none"}
		# the details every removed or pending result reports
		info = {
			"task_id": task_id,
			"path": row["path"],
			"branch": row["branch"],
			"base_branch": row["base_branch"],
		}
		retry = f"progress worktree cleanup {task_id}"
		branch_ref = f"refs/heads/{row['branch']}"

		def pending(reason: str, command: str | None = None) -> dict[str, object]:
			"""Build a pending result for this task with the next command to run.

			Without a command, the next step is to run cleanup again.
			"""
			return {
				"status": "pending",
				"reason": reason,
				"command": command or retry,
				**info,
			}

		try:
			repository = GitRepository(path)
			common_dir = repository.common_dir()
			target, _ = self._record_location(row, project.id, common_dir)

			if not target.exists() and not target.is_symlink():
				base = row["base_branch"]
				unmerged_commits = 0
				count_error = None
				if base is not None:
					count = self._commit_count(
						repository, f"refs/heads/{base}", branch_ref
					)
					if isinstance(count, int):
						unmerged_commits = count
					else:
						count_error = count
				with self.database.transaction() as connection:
					connection.execute(
						"DELETE FROM task_worktrees WHERE task_id = ?", (task_id,)
					)
				gone = {"status": "removed", "reason": "folder already gone", **info}
				if unmerged_commits:
					gone["unmerged_commits"] = unmerged_commits
				if count_error:
					gone["count_error"] = count_error
				if unmerged_commits or count_error:
					gone["command"] = f"git merge {quote(branch_ref)}"
				return gone

			self._validated_record(row, project.id, common_dir)

			resolved_target = target.resolve()
			if Path.cwd().resolve().is_relative_to(
				resolved_target
			) or repository.path.is_relative_to(resolved_target):
				return pending("command uses the task worktree")

			checkout = GitRepository(target)
			changes = checkout.run(
				["status", "--porcelain", "--ignore-submodules=none"]
			)
			if changes.returncode != 0:
				return pending(
					changes.stderr.strip() or "Git could not inspect the worktree"
				)
			if changes.stdout:
				return pending("worktree has uncommitted changes")

			base = row["base_branch"]
			if base is None:
				return pending(
					"base branch was not recorded; run the command from the intended base checkout, then remove the worktree and branch by hand",
					f"git merge {quote(branch_ref)}",
				)
			base_ref = f"refs/heads/{base}"
			base_exists = repository.run(["show-ref", "--verify", "--quiet", base_ref])
			if base_exists.returncode != 0:
				return pending(
					f"base branch {base} no longer exists; run the command from the intended base checkout",
					f"git merge {quote(branch_ref)}",
				)

			merged = self._merge_into_base(repository, checkout, base, row["branch"])
			if isinstance(merged, tuple):
				reason, command = merged
				return pending(reason, command)
			merged_commits = merged

			# Point the task branch at its base so the safe `git branch -d` below
			# checks the merge against the base, not whichever branch the current
			# checkout has open.
			upstream = repository.run(
				["branch", "--set-upstream-to", base, row["branch"]]
			)
			if upstream.returncode != 0:
				return pending(
					upstream.stderr.strip()
					or "Git could not prepare safe branch deletion"
				)

			result = repository.run(["worktree", "remove", str(target)])

			if result.returncode != 0:
				return pending(
					result.stderr.strip() or "Git could not remove the worktree"
				)

			deleted = repository.run(["branch", "-d", row["branch"]])

			with self.database.transaction() as connection:
				connection.execute(
					"DELETE FROM task_worktrees WHERE task_id = ?", (task_id,)
				)
			return {
				"status": "removed",
				"merged_commits": merged_commits,
				**info,
				**(
					{"reason": f"branch could not be deleted: {deleted.stderr.strip()}"}
					if deleted.returncode
					else {}
				),
			}
		except (ProgressError, sqlite3.Error, OSError) as error:
			reason = error.message if isinstance(error, ProgressError) else str(error)
			return pending(reason or type(error).__name__)

	def _commit_count(
		self, repository: GitRepository, base_ref: str, branch_ref: str
	) -> int | str:
		"""Count task commits outside the base, or return Git's error."""
		result = repository.run(["rev-list", "--count", f"{base_ref}..{branch_ref}"])
		if result.returncode != 0:
			return result.stderr.strip() or "Git could not count task commits"
		return int(result.stdout.strip())

	def _merge_into_base(
		self,
		repository: GitRepository,
		checkout: GitRepository,
		base: str,
		branch: str,
	) -> int | tuple[str, str | None]:
		"""Replay the task commits onto the base, then fast-forward the base to them.

		Return the number of commits merged, or a reason and an optional command
		when the checkout must stay.
		"""
		base_ref = f"refs/heads/{base}"
		branch_ref = f"refs/heads/{branch}"
		count = self._commit_count(repository, base_ref, branch_ref)
		if isinstance(count, str):
			return count, None
		if count == 0:
			return 0

		rebase = checkout.run(["rebase", base_ref])
		if rebase.returncode != 0:
			abort = checkout.run(["rebase", "--abort"])
			if abort.returncode != 0:
				return (
					f"rebase failed and abort failed: {abort.stderr.strip()}",
					f"git -C {quote(str(checkout.path))} rebase --abort",
				)
			detail = rebase.stderr.strip() or rebase.stdout.strip()
			label = (
				"rebase conflict"
				if "CONFLICT" in (rebase.stdout + rebase.stderr)
				else "rebase failed"
			)
			return (
				f"{label}: {detail}",
				f"git -C {quote(str(checkout.path))} rebase {quote(base_ref)}",
			)

		replayed = self._commit_count(repository, base_ref, branch_ref)
		if isinstance(replayed, str):
			return replayed, None
		refusal = self._fast_forward_base(repository, checkout, base, branch)
		if refusal is not None:
			return f"fast-forward refused: {refusal}", None
		return replayed

	def _fast_forward_base(
		self, repository: GitRepository, checkout: GitRepository, base: str, branch: str
	) -> str | None:
		"""Fast-forward the base branch to the task branch's rebased commits.

		A base checked out in some worktree is merged there, so Git refuses when
		that checkout's local changes would be overwritten. A base not checked
		out anywhere has its ref moved only if it still points where it did when
		read. Return None on success, or the reason the base was left alone.
		"""
		worktrees = repository.run(["worktree", "list", "--porcelain"])
		if worktrees.returncode != 0:
			return worktrees.stderr.strip() or "Git could not list worktrees"

		base_ref = f"refs/heads/{base}"
		for entry in worktrees.stdout.strip().split("\n\n"):
			lines = entry.splitlines()
			if f"branch {base_ref}" not in lines or not lines[0].startswith(
				"worktree "
			):
				continue

			base_checkout = GitRepository(lines[0][len("worktree ") :])
			result = base_checkout.run(["merge", "--ff-only", f"refs/heads/{branch}"])
			return (
				None
				if result.returncode == 0
				else result.stderr.strip() or result.stdout.strip()
			)

		old = repository.run(["rev-parse", base_ref])
		new = checkout.run(["rev-parse", "HEAD"])
		if old.returncode != 0 or new.returncode != 0:
			return "Git could not read the branch tips"
		ancestor = repository.run(
			["merge-base", "--is-ancestor", old.stdout.strip(), new.stdout.strip()]
		)
		if ancestor.returncode != 0:
			return "base is no longer an ancestor of the task branch"
		result = repository.run(
			["update-ref", base_ref, new.stdout.strip(), old.stdout.strip()]
		)
		return (
			None
			if result.returncode == 0
			else result.stderr.strip() or "base changed while merging"
		)

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
				"task_worktrees.path, task_worktrees.branch, task_worktrees.base_branch, "
				"task_worktrees.common_dir "
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
				"SELECT task_id, project_id, path, branch, base_branch, common_dir "
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
		# Git exits with 1 when HEAD is detached, which leaves the base unknown.
		base_result = repository.run(["symbolic-ref", "--quiet", "--short", "HEAD"])
		if base_result.returncode not in {0, 1}:
			raise GitBindingError(
				f"could not read the base branch: {base_result.stderr.strip()}"
			)
		base_branch = (
			base_result.stdout.strip() if base_result.returncode == 0 else None
		)

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
				"SELECT task_id, project_id, path, branch, base_branch, common_dir "
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
					"(task_id, project_id, path, branch, base_branch, common_dir, created_at) "
					"VALUES (?, ?, ?, ?, ?, ?, ?)",
					(
						task_id,
						project.id,
						str(target),
						branch,
						base_branch,
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
				"base_branch": base_branch,
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
			"base_branch": row["base_branch"],
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
