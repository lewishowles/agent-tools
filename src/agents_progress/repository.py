"""Git repository discovery and local progress project bindings."""

from pathlib import Path
import subprocess

from .errors import GitBindingError, NotAProjectError

# the local Git config key that stores a repository's bound project ID
_BINDING_KEY = "progress.project-id"


class GitRepository:
	"""Read and write a progress binding in a repository's local Git config."""

	def __init__(self, path: str | Path | None = None) -> None:
		candidate = Path(path) if path is not None else Path.cwd()
		self.path = candidate.expanduser().resolve()

	def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
		"""Run a Git command in this repository's path and return the result, raising NotAProjectError if Git cannot be launched."""
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
		result = self._run(["rev-parse", "--show-toplevel"])
		if result.returncode != 0:
			raise NotAProjectError(
				f"{self.path} is not inside a Git repository",
				{"path": str(self.path)},
			)

		return Path(result.stdout.strip()).resolve()

	def get_binding(self) -> str | None:
		"""Read the local progress project ID, if one is configured, raising NotAProjectError or GitBindingError on failure."""
		self.root()
		result = self._run(["config", "--local", "--get", _BINDING_KEY])
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
		result = self._run(["config", "--local", _BINDING_KEY, project_id])
		if result.returncode != 0:
			raise GitBindingError(
				f"could not write {_BINDING_KEY}: {result.stderr.strip()}",
				{"key": _BINDING_KEY, "project_id": project_id},
			)

	def clear_binding(self) -> None:
		"""Remove the local progress project ID when it exists."""
		self.root()
		result = self._run(["config", "--local", "--unset-all", _BINDING_KEY])
		# 0 = removed, 1 = key was not present, 5 = --unset-all found nothing to unset;
		# all three mean the binding is already clear, so clear_binding stays idempotent
		if result.returncode in {0, 1, 5}:
			return
		if result.returncode != 0:
			raise GitBindingError(
				f"could not clear {_BINDING_KEY}: {result.stderr.strip()}",
				{"key": _BINDING_KEY},
			)
