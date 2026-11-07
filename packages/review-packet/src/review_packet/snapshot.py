"""Record the Git changes a review covers."""

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# The base commit text used when the repository has no commits yet.
NO_COMMITS = "(no commits)"


class InvalidBaseError(ValueError):
	"""The requested base does not name a commit."""


class GitError(RuntimeError):
	"""A Git command failed."""


class ChangeStatus(StrEnum):
	"""How a path differs from the base."""

	# The file was added to Git after the base.
	ADDED = "added"
	# The tracked file has different contents or metadata now.
	CHANGED = "changed"
	# The file existed at the base and has since been removed.
	DELETED = "deleted"
	# The file moved from another path, possibly with edits.
	RENAMED = "renamed"
	# The file is new and Git does not track it yet.
	UNTRACKED = "untracked"


# Git status letters that map to their own change status. Any other letter
# means the file changed.
STATUS_BY_LETTER = {"A": ChangeStatus.ADDED, "D": ChangeStatus.DELETED}


@dataclass(frozen=True)
class Change:
	"""One path that differs from the base."""

	# The repository-relative path as it is now.
	path: str
	# How the path differs from the base.
	status: ChangeStatus
	# The path before a rename, or None for every other change.
	old_path: str | None = None


@dataclass(frozen=True)
class Snapshot:
	"""The changes a review covers, pinned to an exact base commit."""

	# The revision the user asked for, or HEAD when they named none.
	base: str
	# The commit that revision pointed to, or None before the first commit.
	base_sha: str | None
	# Whether the worktree has staged, unstaged or untracked changes.
	uncommitted: bool
	# Every changed path, sorted by path.
	changes: tuple[Change, ...]


def repository_root(cwd: Path) -> Path:
	"""Find the root folder of the worktree containing a folder.

	Args:
		cwd: Any folder inside the worktree.

	Returns:
		The absolute worktree root.

	Raises:
		GitError: The folder is not inside a Git worktree.
	"""
	return Path(_git_text(cwd, "rev-parse", "--show-toplevel")).resolve()


def repository_name(root: Path) -> str:
	"""Name the repository, giving every worktree of it the same name.

	A worktree's own folder is often named after its branch or task, so the
	name comes from the folder that holds the main checkout's `.git` instead.

	Args:
		root: The worktree root.

	Returns:
		The main checkout's folder name.

	Raises:
		GitError: Git cannot find the shared `.git` folder.
	"""
	# The `.git` folder shared by the main checkout and all its worktrees.
	common = Path(
		_git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
	)

	return common.parent.name


def capture(root: Path, base: str | None = None) -> Snapshot:
	"""List the changes since a base revision.

	Args:
		root: The worktree root.
		base: The revision to compare against, or None for the last commit.

	Returns:
		The changed paths and the resolved base commit.

	Raises:
		InvalidBaseError: The requested base does not name a commit.
		GitError: A Git command failed.
	"""
	# The resolved base commit, or None before the first commit.
	base_sha = _resolve_base(root, base)

	# Every changed file, tracked or not. Git never reports one path as both.
	changes = _changed_since_base(root, base_sha) + _untracked(root)
	# The changes looked up by path, so they can be listed in path order.
	by_path = {change.path: change for change in changes}
	# The changes sorted by path, so the brief and manifest are repeatable.
	ordered = tuple(by_path[path] for path in sorted(by_path))

	return Snapshot(
		base or "HEAD",
		base_sha,
		bool(_git(root, "status", "--porcelain", "-z")),
		ordered,
	)


def is_ignored(root: Path, path: Path) -> bool:
	"""Check whether Git excludes a file from the repository.

	Args:
		root: The repository root.
		path: A file inside the repository.

	Returns:
		True when Git ignores the file. A Git error counts as not ignored.
	"""
	# Git exits with one for a file that is not ignored, so _git cannot run this check.
	result = subprocess.run(
		["git", "check-ignore", "-q", "--", str(path.relative_to(root))],
		cwd=root,
		check=False,
		capture_output=True,
	)

	return result.returncode == 0


def repository_files(root: Path) -> tuple[str, ...]:
	"""List the repository's tracked files and its new files that Git does not ignore.

	Args:
		root: The repository root.

	Returns:
		Repository-relative paths, including tracked files deleted from disk.

	Raises:
		GitError: Git could not list the files.
	"""
	return tuple(
		_split_paths(
			_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
		)
	)


def _resolve_base(root: Path, base: str | None) -> str | None:
	"""Turn the requested base, or HEAD, into a commit SHA.

	Recording the SHA keeps the brief accurate after a branch name moves.

	Args:
		root: The worktree root.
		base: The requested revision, or None for HEAD.

	Returns:
		The commit SHA, or None when no base was requested and the
		repository has no commits yet.

	Raises:
		InvalidBaseError: The requested base does not name a commit.
	"""
	# The revision to look up, with `^{commit}` so a tag resolves to its commit.
	revision = f"{base or 'HEAD'}^{{commit}}"

	try:
		return _git_text(root, "rev-parse", "--verify", revision)
	except GitError as error:
		if base:
			raise InvalidBaseError(f"Invalid base revision: {base}") from error

		return None


def _changed_since_base(root: Path, base_sha: str | None) -> list[Change]:
	"""List tracked paths that differ from the base, staged or not.

	Args:
		root: The worktree root.
		base_sha: The base commit, or None before the first commit.

	Returns:
		The tracked changes. Before the first commit, every staged file is added.

	Raises:
		GitError: A Git command failed.
	"""
	if base_sha is None:
		return [
			Change(path, ChangeStatus.ADDED)
			for path in _split_paths(_git(root, "ls-files", "-z"))
		]

	return _parse_changes(
		_git(root, "diff", "--name-status", "-z", "--find-renames", base_sha)
	)


def _untracked(root: Path) -> list[Change]:
	"""List new files that Git does not track and does not ignore.

	Args:
		root: The worktree root.

	Returns:
		The untracked files.

	Raises:
		GitError: A Git command failed.
	"""
	# New paths outside the index, separated by NUL for unusual file names.
	output = _git(root, "ls-files", "--others", "--exclude-standard", "-z")

	return [Change(path, ChangeStatus.UNTRACKED) for path in _split_paths(output)]


def _parse_changes(output: bytes) -> list[Change]:
	"""Read the output of `git diff --name-status -z`.

	Each record is a status letter followed by one path, or by the old and
	new paths for a rename.

	Args:
		output: The raw Git output.

	Returns:
		One change per record.
	"""
	# Status letters and paths, in the order Git printed them.
	fields = iter(_split_paths(output))
	# One change per Git status record.
	changes = []

	for status in fields:
		if status.startswith("R"):
			old_path = next(fields)
			changes.append(Change(next(fields), ChangeStatus.RENAMED, old_path))
			continue

		# The status for the record's first letter; Git can follow it with a score.
		change_status = STATUS_BY_LETTER.get(status[0], ChangeStatus.CHANGED)
		changes.append(Change(next(fields), change_status))

	return changes


def _split_paths(output: bytes) -> list[str]:
	"""Split NUL-separated Git output into fields, keeping odd file names intact.

	Args:
		output: The raw Git output.

	Returns:
		The non-empty fields.
	"""
	return [
		field for field in output.decode(errors="surrogateescape").split("\0") if field
	]


def _git_text(root: Path, *args: str) -> str:
	"""Run Git and return its output as one line of text.

	Args:
		root: The folder to run Git in.
		*args: The Git arguments.

	Returns:
		The output without surrounding whitespace.

	Raises:
		GitError: Git exited with an error.
	"""
	return _git(root, *args).decode().strip()


def _git(root: Path, *args: str) -> bytes:
	"""Run Git and return its raw output.

	Args:
		root: The folder to run Git in.
		*args: The Git arguments.

	Returns:
		The bytes Git printed.

	Raises:
		GitError: Git exited with an error, with Git's own message.
	"""
	result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)

	if result.returncode:
		raise GitError(result.stderr.decode(errors="replace").strip() or "Git failed.")

	return result.stdout
