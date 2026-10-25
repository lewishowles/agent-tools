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
		self,
		task_id: str | None = None,
		path: str | Path | None = None,
		*,
		branch: str | None = None,
	) -> dict[str, object]:
		"""Merge a done task's branch into its base, then remove its checkout.

		The task's commits are replayed onto the base branch recorded when the
		checkout was created, the base is fast-forwarded to them, the checkout is
		removed and the merged task branch is deleted. Return status "removed"
		with the number of commits merged, "pending" with a reason, or "none" when
		the task has no recorded checkout or managed branch. Removed and pending
		results include the path, task branch and base branch.

		A checkout with uncommitted changes, an unknown or missing base, a rebase
		conflict, a locked checkout, a refused fast-forward, or a checkout that
		contains the current folder or the given path stays on disk with its branch
		and record, so cleanup can be retried later. After a rebase conflict, a
		rerun carries on from the pause once the clashing files are fixed. A
		removal failure can be pending after the commits reached the base; retry
		then only removes the checkout. A recorded folder that no longer exists is
		restored before cleanup continues.

		When the task has no checkout record but its managed branch exists, the
		branch checked out where cleanup runs becomes the base, and a missing
		checkout folder is recreated first. Pass branch instead of task_id to
		finish a managed branch whose task record has been deleted.

		Raise when the task is missing or not done, when a given branch is not a
		managed task branch, does not exist, or still has a task record, or when
		cleanup runs on the task branch itself or outside any branch.
		"""
		project = self.current_project(path)
		task_id, row, retry, early_result = self._cleanup_target(
			task_id, branch, path, project.id
		)
		if early_result is not None:
			return early_result
		# the details every removed or pending result reports
		info = {
			"task_id": task_id,
			"path": row["path"],
			"branch": row["branch"],
			"base_branch": row["base_branch"],
		}
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

			folder_was_missing = not target.exists() and not target.is_symlink()
			if folder_was_missing and not self._restore_missing_checkout(
				repository, target, row["branch"]
			):
				return pending(f"could not recreate the task worktree at {target}")

			checkout = GitRepository(target)
			rebase_in_progress = self._rebase_in_progress(checkout, row["branch"])
			self._validated_record(
				row, project.id, common_dir, allow_detached_replay=rebase_in_progress
			)

			resolved_target = target.resolve()
			if Path.cwd().resolve().is_relative_to(
				resolved_target
			) or repository.path.is_relative_to(resolved_target):
				return pending("command uses the task worktree")

			changes = checkout.run(
				["status", "--porcelain", "--ignore-submodules=none"]
			)
			if changes.returncode != 0:
				return pending(
					changes.stderr.strip() or "Git could not inspect the worktree"
				)
			if changes.stdout and not rebase_in_progress:
				return pending("worktree has uncommitted changes")

			base = row["base_branch"]
			if base is None:
				return pending(
					"base branch was not recorded; run the command from the intended base checkout, then remove the worktree and branch by hand",
					None if folder_was_missing else f"git merge {quote(branch_ref)}",
				)
			base_ref = f"refs/heads/{base}"
			base_exists = repository.run(["show-ref", "--verify", "--quiet", base_ref])
			if base_exists.returncode != 0:
				return pending(
					f"base branch {base} no longer exists; run the command from the intended base checkout",
					None if folder_was_missing else f"git merge {quote(branch_ref)}",
				)

			merged = self._merge_into_base(
				repository, checkout, base, row["branch"], rebase_in_progress
			)
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

	def _cleanup_target(
		self,
		task_id: str | None,
		branch: str | None,
		path: str | Path | None,
		project_id: str,
	) -> tuple[
		str, sqlite3.Row | dict[str, str | None] | None, str, dict[str, object] | None
	]:
		"""Choose what cleanup works on: a task's recorded checkout or its managed branch.

		Return the task ID, the checkout row, the command that retries this cleanup,
		and an early result. When no checkout is recorded, the row is an in-memory
		stand-in built from the managed branch, and a missing folder is recreated
		first. The early result is set, and the row is None, when there is nothing
		to clean up or the folder could not be recreated.
		"""
		if branch is not None:
			try:
				task_id = validate_object_id(
					branch.removeprefix("progress/task/"), TASK_PREFIX
				)
			except ProgressError as error:
				raise ProgressError(f"{branch} is not a managed task branch") from error
			if branch != f"progress/task/{task_id}":
				raise ProgressError(f"{branch} is not a managed task branch")
			with self.database.connection() as connection:
				if connection.execute(
					"SELECT 1 FROM tasks WHERE id = ?", (task_id,)
				).fetchone():
					raise ProgressError(
						f"task {task_id} still exists; use progress worktree cleanup {task_id}"
					)
			row = None
			retry = f"progress worktree cleanup --branch {branch}"
		else:
			task_id = validate_object_id(task_id, TASK_PREFIX)
			with self.database.connection() as connection:
				task = connection.execute(
					"SELECT status FROM tasks WHERE id = ? AND project_id = ?",
					(task_id, project_id),
				).fetchone()
				if task is None:
					raise NotFoundError(
						f"task {task_id} was not found", {"id": task_id}
					)
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
			retry = f"progress worktree cleanup {task_id}"

		if row is not None:
			return task_id, row, retry, None
		target, managed_branch, exists, base = self._unrecorded_branch(
			task_id, project_id, path
		)
		if not exists:
			if branch is not None:
				raise NotFoundError(
					f"branch {managed_branch} was not found", {"branch": managed_branch}
				)
			return task_id, None, retry, {"status": "none"}
		if base is None:
			raise ProgressError("Run cleanup from the intended base branch checkout")
		repository = GitRepository(path)
		folder_was_missing = not target.exists() and not target.is_symlink()
		if folder_was_missing and not self._restore_missing_checkout(
			repository, target, managed_branch
		):
			return (
				task_id,
				None,
				retry,
				{
					"status": "pending",
					"task_id": task_id,
					"path": str(target),
					"branch": managed_branch,
					"base_branch": base,
					"reason": f"could not recreate the task worktree at {target}",
					"command": retry,
				},
			)
		row = {
			"task_id": task_id,
			"project_id": project_id,
			"path": str(target),
			"branch": managed_branch,
			"base_branch": base,
			"common_dir": str(repository.common_dir()),
		}
		return task_id, row, retry, None

	def _unrecorded_branch(
		self, task_id: str, project_id: str, path: str | Path | None
	) -> tuple[Path, str, bool, str | None]:
		"""Describe a task's managed branch when no checkout is recorded for it.

		Return the managed folder, the branch name, whether the branch exists, and
		the branch checked out where the command runs, which serves as the base.
		The base is None when HEAD is detached or is the task branch itself. Raise
		when Git cannot check the branch.
		"""
		target, branch = self._managed_location(project_id, task_id)
		repository = GitRepository(path)
		branch_result = repository.run(
			["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
		)
		if branch_result.returncode not in (0, 1):
			raise GitBindingError(f"Git could not check task branch {branch}")
		exists = branch_result.returncode == 0
		if not exists:
			return target, branch, False, None
		base_result = repository.run(["symbolic-ref", "--quiet", "--short", "HEAD"])
		base = base_result.stdout.strip()
		if base_result.returncode != 0 or base == branch:
			base = None
		return target, branch, True, base

	def _restore_missing_checkout(
		self, repository: GitRepository, target: Path, branch: str
	) -> bool:
		"""Recreate a missing managed checkout without changing other Git worktrees.

		Return whether the checkout was recreated.
		"""
		try:
			worktrees = repository.run(["worktree", "list", "--porcelain"])
			prunable = worktrees.returncode == 0 and any(
				entry.splitlines()[0] == f"worktree {target}"
				and any(
					line == "prunable" or line.startswith("prunable ")
					for line in entry.splitlines()[1:]
				)
				for entry in worktrees.stdout.strip().split("\n\n")
				if entry
			)
			arguments = ["worktree", "add"]
			if prunable:
				arguments.append("-f")
			return repository.run([*arguments, str(target), branch]).returncode == 0
		except (ProgressError, OSError):
			return False

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
		rebase_in_progress: bool,
	) -> int | tuple[str, str | None]:
		"""Replay the task commits onto the base, then fast-forward the base to them.

		When an earlier cleanup paused on clashes, carry on with that replay once
		the human has fixed them, instead of starting again. Return the number of
		commits merged, or a reason and an optional command when the checkout
		must stay.
		"""
		base_ref = f"refs/heads/{base}"
		branch_ref = f"refs/heads/{branch}"
		count = self._commit_count(repository, base_ref, branch_ref)
		if isinstance(count, str):
			return count, None
		if count == 0:
			return 0

		if rebase_in_progress:
			conflicts = self._unmerged_paths(checkout)
			if isinstance(conflicts, str):
				return conflicts, None
			marked = [
				path
				for path in conflicts
				if self._has_conflict_markers(checkout.path / path)
			]
			if marked:
				return self._conflict_reason(base, checkout, marked), None
			if (
				conflicts
				and checkout.run(["add", "-A", "--", *conflicts]).returncode != 0
			):
				return f"could not prepare the fixed files at {checkout.path}", None
			# Keep each commit's message without opening an editor, even when
			# the user's environment names one.
			rebase = checkout.run(
				["rebase", "--continue"], environment={"GIT_EDITOR": "true"}
			)
		else:
			rebase = checkout.run(["rebase", base_ref])
		if rebase.returncode != 0:
			conflicts = self._unmerged_paths(checkout)
			if isinstance(conflicts, list) and conflicts:
				return self._conflict_reason(base, checkout, conflicts), None
			if rebase_in_progress:
				return (
					f"could not continue the paused replay onto {base} at {checkout.path}",
					None,
				)
			abort = checkout.run(["rebase", "--abort"])
			if abort.returncode != 0:
				return (
					f"could not replay the task onto {base} or stop the replay at {checkout.path}",
					None,
				)
			return f"could not replay the task onto {base}", None

		replayed = self._commit_count(repository, base_ref, branch_ref)
		if isinstance(replayed, str):
			return replayed, None
		refusal = self._fast_forward_base(repository, checkout, base, branch)
		if refusal is not None:
			return f"fast-forward refused: {refusal}", None
		return replayed

	def _rebase_in_progress(self, checkout: GitRepository, branch: str) -> bool:
		"""Report whether a replay of the task branch is paused part way."""
		for name in ("rebase-merge", "rebase-apply"):
			result = checkout.run(["rev-parse", "--git-path", name])
			if result.returncode != 0:
				continue
			head_name = Path(result.stdout.strip()) / "head-name"
			if (
				head_name.is_file()
				and head_name.read_text().strip() == f"refs/heads/{branch}"
			):
				return True
		return False

	def _unmerged_paths(self, checkout: GitRepository) -> list[str] | str:
		"""List the files that still clash, relative to the checkout.

		Return a reason instead when Git cannot list them.
		"""
		result = checkout.run(["diff", "--name-only", "--diff-filter=U", "-z"])
		if result.returncode != 0:
			return f"could not check clashing files at {checkout.path}"
		return [path for path in result.stdout.split("\0") if path]

	def _has_conflict_markers(self, path: Path) -> bool:
		"""Report whether the file still has clash marker lines.

		A line of equals signs alone does not count, because Markdown uses one to
		underline a heading.
		"""
		if not path.is_file():
			return False
		with path.open(errors="replace") as file:
			return any(line.startswith(("<<<<<<<", ">>>>>>>")) for line in file)

	def _conflict_reason(
		self, base: str, checkout: GitRepository, files: list[str]
	) -> str:
		"""Build the pause message that tells the human where to fix the clashes.

		Files that cannot show clash markers, such as binary files or a file
		deleted on one side, are named separately, because whatever the checkout
		holds for them when cleanup reruns is kept as it is.
		"""
		# The Git merge stages present for each file; a file missing either
		# side's stage was deleted on that side.
		stages: dict[str, set[str]] = {}
		entries = checkout.run(["ls-files", "--unmerged", "-z", "--", *files])

		if entries.returncode == 0:
			for entry in entries.stdout.split("\0"):
				if entry:
					metadata, file = entry.split("\t", 1)
					stages.setdefault(file, set()).add(metadata.rsplit(" ", 1)[-1])

		# The clashing files that cannot hold marker lines.
		without_markers = []
		for file in files:
			file_path = checkout.path / file
			if (
				stages.get(file) and not {"2", "3"}.issubset(stages[file])
			) or not file_path.is_file():
				without_markers.append(file)
			else:
				with file_path.open("rb") as contents:
					sample = contents.read(8192)
				try:
					sample.decode("utf-8")
				except UnicodeDecodeError:
					without_markers.append(file)
				else:
					if b"\0" in sample:
						without_markers.append(file)

		reason = (
			f"could not replay the task onto {base}; fix the clashes in "
			f"{', '.join(files)} at {checkout.path}"
		)
		if without_markers:
			reason += (
				f". These files cannot show clash markers: {', '.join(without_markers)}. "
				"The version in the task folder when you rerun cleanup, including a "
				"deletion, is the one that will be kept"
			)

		return f"{reason}. Then rerun the cleanup command below"

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
		longer exists, "removable", or "kept" with a plain reason. A done task with
		commits outside its base stays kept even with force. With force, a
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
		# a task with no saved checkout can still have a managed branch to protect
		unrecorded = row["path"] is None
		if unrecorded:
			if row["status"] != "done":
				return {"status": "none"}
			try:
				managed_path, branch, exists, base_branch = self._unrecorded_branch(
					task_id, project.id, path
				)
			except (ProgressError, OSError) as error:
				managed_path, _ = self._managed_location(project.id, task_id)
				reason = (
					error.message if isinstance(error, ProgressError) else str(error)
				)
				return {
					"status": "kept",
					"path": str(managed_path),
					"reason": f"could not check the task branch ({reason}); run progress worktree cleanup {task_id} first",
				}
			if not exists:
				return {"status": "none"}
			checkout_path = str(managed_path)
		else:
			checkout_path = row["path"]
			if Path(checkout_path) != self._managed_location(project.id, task_id)[0]:
				return {
					"status": "unsafe" if force else "kept",
					"path": checkout_path,
					"reason": (
						"outside the managed location"
						if force
						else "recorded path is outside the managed location"
					),
				}
			try:
				repository = GitRepository(path)
				self._record_location(row, project.id, repository.common_dir())
			except (ProgressError, OSError) as error:
				reason = (
					error.message if isinstance(error, ProgressError) else str(error)
				)
				return {
					"status": "unsafe" if force else "kept",
					"path": checkout_path,
					"reason": reason,
				}
			branch = row["branch"]
			base_branch = row["base_branch"]
		if row["status"] == "done":
			# count holds the number of unmerged commits, or the reason it could
			# not be counted, which also keeps the task
			try:
				if base_branch is None:
					count = "the base branch is unknown"
				else:
					repository = GitRepository(path)
					count = self._commit_count(
						repository,
						f"refs/heads/{base_branch}",
						f"refs/heads/{branch}",
					)
			except (ProgressError, OSError) as error:
				count = (
					error.message if isinstance(error, ProgressError) else str(error)
				)
			if count != 0:
				reason = (
					f"{count} task {'commit is' if count == 1 else 'commits are'} not on the base branch"
					if isinstance(count, int)
					else f"could not check task commits against the base branch ({count})"
				)
				return {
					"status": "kept",
					"path": checkout_path,
					"reason": f"{reason}; run progress worktree cleanup {task_id} first",
				}
		if unrecorded:
			return {"status": "none"}
		if (
			force
			and not Path(checkout_path).exists()
			and not Path(checkout_path).is_symlink()
		):
			return {"status": "gone", "path": checkout_path}

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
		self,
		row: sqlite3.Row,
		project_id: str,
		common_dir: Path,
		*,
		allow_detached_replay: bool = False,
	) -> dict[str, str]:
		"""Return the saved task, project, path, and branch if they still match.

		Refuse a record whose project, path, branch, or repository no longer match.
		With allow_detached_replay, also accept a checkout with no branch, which
		is how Git leaves it while a replay of the task branch is paused.
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

		if not (
			(result.returncode == 0 and result.stdout.strip() == branch)
			or (allow_detached_replay and result.returncode == 1)
		):
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
