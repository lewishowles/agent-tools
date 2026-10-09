"""Git repository discovery and local progress project bindings."""

import subprocess
from pathlib import Path

from .errors import GitBindingError, NotAProjectError

# Git config key that binds a repository to its progress project.
_BINDING_KEY = "progress.project-id"
# The most changed paths a start lists; the count still covers every change.
_MAX_UNCOMMITTED_PATHS = 20


class GitRepository:
	"""Read and write a progress binding in a repository's local Git config."""

	def __init__(self, path: str | Path | None = None) -> None:
		"""Store the resolved path used for Git commands."""
		candidate = Path(path) if path is not None else Path.cwd()
		self.path = candidate.expanduser().resolve()

	def run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
		"""Return the completed Git process without raising for a non-zero exit.

		Raise NotAProjectError if Git cannot be launched.
		"""
		try:
			return subprocess.run(
				["git", "-C", str(self.path), *arguments],
				capture_output=True,
				check=False,
				text=True,
			)
		except OSError as error:
			raise NotAProjectError(
				f"could not run Git for {self.path}: {error}",
				{"path": str(self.path)},
			) from error

	def root(self) -> Path:
		"""Return the repository root or raise for a non-Git path."""
		result = self.run(["rev-parse", "--show-toplevel"])
		if result.returncode != 0:
			raise NotAProjectError(
				f"{self.path} is not inside a Git repository",
				{"path": str(self.path)},
			)

		return Path(result.stdout.strip()).resolve()

	def common_dir(self) -> Path:
		"""Return the Git directory shared by every worktree of this repository."""
		self.root()
		result = self.run(["rev-parse", "--git-common-dir"])
		if result.returncode != 0:
			raise NotAProjectError(
				f"could not find the Git common directory for {self.path}: {result.stderr.strip()}",
				{"path": str(self.path)},
			)

		return (self.path / result.stdout.strip()).resolve()

	def uncommitted_changes(self) -> dict[str, object]:
		"""Report the worktree's staged, unstaged, and untracked entries as Git lists them.

		The result has a status of clean, dirty, or unavailable, the full entry
		count, and a capped list of paths. A failed or undecodable status query
		returns unavailable with a short reason, so a start can still go ahead.
		"""
		try:
			result = self.run(["status", "--porcelain=v1", "-z"])
		except UnicodeDecodeError:
			return {
				"status": "unavailable",
				"count": 0,
				"paths": [],
				"reason": "Git status output is not valid UTF-8",
			}

		if result.returncode != 0:
			return {
				"status": "unavailable",
				"count": 0,
				"paths": [],
				"reason": result.stderr.strip()
				or f"Git exited with status {result.returncode}",
			}

		count = 0
		paths = []
		entries = iter(result.stdout.split("\0"))
		for entry in entries:
			if not entry:
				continue

			count += 1
			# Each entry starts with a two-letter status and a space before the path.
			if len(paths) < _MAX_UNCOMMITTED_PATHS:
				paths.append(entry[3:])
			# A rename or copy is followed by its original path, which is not a separate change.
			if "R" in entry[:2] or "C" in entry[:2]:
				next(entries, None)

		return {"status": "dirty" if count else "clean", "count": count, "paths": paths}

	def get_binding(self) -> str | None:
		"""Read the local progress project ID, if one is configured, raising NotAProjectError or GitBindingError on failure."""
		self.root()
		result = self.run(["config", "--local", "--get", _BINDING_KEY])
		if result.returncode == 1:
			return None
		if result.returncode != 0:
			raise GitBindingError(
				f"could not read {_BINDING_KEY}: {result.stderr.strip()}",
				{"key": _BINDING_KEY},
			)

		return result.stdout.strip()

	def set_binding(self, project_id: str) -> None:
		"""Set the local progress project ID, raising NotAProjectError or GitBindingError on failure."""
		self.root()
		result = self.run(["config", "--local", _BINDING_KEY, project_id])
		if result.returncode != 0:
			raise GitBindingError(
				f"could not write {_BINDING_KEY}: {result.stderr.strip()}",
				{"key": _BINDING_KEY, "project_id": project_id},
			)

	def clear_binding(self) -> None:
		"""Remove the local progress project ID when it exists."""
		self.root()
		result = self.run(["config", "--local", "--unset-all", _BINDING_KEY])
		# 0 = removed, 1 = key was not present, 5 = --unset-all found nothing to unset;
		# all three mean the binding is already clear, so clear_binding stays idempotent
		if result.returncode in {0, 1, 5}:
			return
		if result.returncode != 0:
			raise GitBindingError(
				f"could not clear {_BINDING_KEY}: {result.stderr.strip()}",
				{"key": _BINDING_KEY},
			)
