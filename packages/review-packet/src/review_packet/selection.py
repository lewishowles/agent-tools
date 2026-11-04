"""Choose repository files for the draft packet."""

import posixpath
from pathlib import Path

from review_packet.snapshot import ChangeStatus, Snapshot

# Files that describe how a project is built and tested. Each changed file
# brings in any of these from its nearest project folder.
PROJECT_FILES = (
	"pyproject.toml",
	"package.json",
	"README.md",
	"pytest.ini",
	"vitest.config.ts",
	"vitest.config.js",
	"vitest.config.mts",
)

# Files every packet includes when the repository has them.
REPOSITORY_FILES = ("AGENTS.md", "README.md")

# Files whose presence marks a folder as a Python or JavaScript project.
MANIFEST_FILES = ("pyproject.toml", "package.json")


class MissingIncludeError(ValueError):
	"""An explicitly included file is missing or outside the repository."""


def select_files(
	root: Path, snapshot: Snapshot, extra: tuple[str, ...]
) -> tuple[str, ...]:
	"""Choose the files a reviewer needs to read.

	Args:
		root: The repository root.
		snapshot: The current changes.
		extra: Files the user asked to include.

	Returns:
		The changed files that still exist, the extra files, AGENTS.md, the
		root README.md, and the project files nearest each change, sorted
		by path.
		Candidates that do not exist are skipped.

	Raises:
		MissingIncludeError: An extra file is missing or outside the repository.
	"""
	# Every candidate path, before checking that it exists.
	paths = {
		change.path
		for change in snapshot.changes
		if change.status != ChangeStatus.DELETED
	}

	# Each included path in normal form, mapped to how the user wrote it.
	includes = _normalise_includes(extra)

	paths.update(includes)
	paths.update(REPOSITORY_FILES)

	for change in snapshot.changes:
		paths.update(_project_files(root, change.path))

	# The candidates that exist inside the repository.
	selected = []

	for path in sorted(paths):
		if _is_repository_file(root, path):
			selected.append(path)
		elif path in includes:
			raise MissingIncludeError(
				f"Included file is missing or outside the repository: {includes[path]}"
			)

	return tuple(selected)


def _normalise_includes(extra: tuple[str, ...]) -> dict[str, str]:
	"""Put included paths in normal form so ./README.md and README.md match.

	Args:
		extra: Paths the user asked to include.

	Returns:
		Each path in normal form, mapped to how the user wrote it, so an
		error can quote the user's own path.
	"""
	# How the user wrote each path, keyed by its normal form.
	includes = {}

	for path in extra:
		includes[posixpath.normpath(path)] = path

	return includes


def _project_files(root: Path, changed_path: str) -> list[str]:
	"""List the project file candidates for one changed file.

	Args:
		root: The repository root.
		changed_path: A repository-relative changed file.

	Returns:
		Repository-relative paths in the nearest project folder, or an empty
		list when no folder up to the root has a manifest.
	"""
	# The changed file's project folder, if it has one.
	folder = _nearest_project_folder(root, changed_path)

	if folder is None:
		return []

	return [str((folder / name).relative_to(root)) for name in PROJECT_FILES]


def _is_repository_file(root: Path, path: str) -> bool:
	"""Check that a path is a file inside the repository.

	Args:
		root: The repository root.
		path: A repository-relative path.

	Returns:
		True when the path is a file and, after following links, stays inside
		the repository.
	"""
	# The path as given, which must be relative and stay below the root.
	relative_path = Path(path)

	if relative_path.is_absolute() or ".." in relative_path.parts:
		return False

	# The path on disk.
	candidate = root / path

	return candidate.is_file() and candidate.resolve().is_relative_to(root.resolve())


def _nearest_project_folder(root: Path, changed_path: str) -> Path | None:
	"""Find the nearest folder above a file that has a project manifest.

	Args:
		root: The repository root.
		changed_path: A repository-relative changed file.

	Returns:
		The nearest project folder, or None if there is no manifest up to and
		including the root.
	"""
	# The folder being checked, starting next to the changed file.
	folder = (root / changed_path).parent

	while folder.is_relative_to(root):
		if any((folder / name).is_file() for name in MANIFEST_FILES):
			return folder

		if folder == root:
			break

		folder = folder.parent

	return None
