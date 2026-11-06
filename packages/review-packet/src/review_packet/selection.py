"""Choose repository files for the draft packet."""

import posixpath
from dataclasses import dataclass
from pathlib import Path

from review_packet.imports import IMPORT_SOURCE_SUFFIXES, find_imports, repository_path
from review_packet.snapshot import ChangeStatus, Snapshot, is_ignored

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


class OutsideSymlinkError(ValueError):
	"""A selected path leads outside the repository through a link."""


@dataclass(frozen=True)
class Selection:
	"""The packet's files and what the brief lists under Left out."""

	# Repository-relative paths of the files the packet holds, sorted.
	files: tuple[str, ...]
	# Local imports with no file, each as "<changed file>: <import>".
	unresolved: tuple[str, ...]
	# Changed files whose imports could not be read.
	unreadable: tuple[str, ...]
	# Imported or matched test files that link outside the repository.
	outside: tuple[str, ...]


@dataclass(frozen=True)
class _ChangedContext:
	"""What the changed files bring into the packet, and what they leave out."""

	# Candidate files: project files, direct imports and matching tests.
	paths: set[str]
	# Local imports with no file, each as "<changed file>: <import>".
	unresolved: set[str]
	# Changed files whose imports could not be read.
	unreadable: set[str]
	# Imported or matched test files that link outside the repository.
	outside: set[str]


def select_files(root: Path, snapshot: Snapshot, extra: tuple[str, ...]) -> Selection:
	"""Choose packet files and what the brief lists under Left out.

	Args:
		root: The repository root.
		snapshot: The current changes.
		extra: Files the user asked to include.

	Returns:
		The selected files in sorted order, with candidates that don't exist
		skipped: changed files, extra files, AGENTS.md, the root README.md, each
		change's project files, and its direct imports and matching tests. Also
		the missing local imports, files whose imports could not be read, and
		imported or test files that link outside the repository.

	Raises:
		MissingIncludeError: An extra file is missing or outside the repository.
		OutsideSymlinkError: A selected path leads outside the repository through a link.
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
	# Files and omissions found from changed paths.
	context = _changed_context(root, snapshot)
	paths.update(context.paths)

	# Paths that reach outside the repository through a link at any level, which
	# can't be shared. They are checked first so an included link gets this error
	# rather than a missing-file one. Absolute paths and paths that climb out with
	# .. are left for the missing-include check below.
	outside_links = [
		path
		for path in sorted(paths)
		if not Path(path).is_absolute()
		and ".." not in Path(path).parts
		and _leads_outside(root, path)
	]

	if outside_links:
		raise OutsideSymlinkError(
			"Paths that lead outside the repository through a link can't be shared: "
			+ ", ".join(outside_links)
		)

	# The candidates that exist inside the repository.
	selected = []

	for path in sorted(paths):
		if _is_repository_file(root, path):
			selected.append(path)
		elif path in includes:
			raise MissingIncludeError(
				f"Included file is missing or outside the repository: {includes[path]}"
			)

	return Selection(
		tuple(selected),
		tuple(sorted(context.unresolved)),
		tuple(sorted(context.unreadable)),
		tuple(sorted(context.outside)),
	)


def _changed_context(root: Path, snapshot: Snapshot) -> _ChangedContext:
	"""Gather project files, direct imports and tests for changed files.

	Args:
		root: The repository root.
		snapshot: The current changes.

	Returns:
		Candidate paths, missing import labels, unreadable source paths, and
		imported or test files that link outside the repository.
	"""
	# Files gathered from the changed paths.
	paths: set[str] = set()
	# Local imports with no file, each as "<changed file>: <import>".
	unresolved: set[str] = set()
	# Changed files whose imports could not be read.
	unreadable: set[str] = set()
	# Imported or matched test files that link outside the repository.
	outside: set[str] = set()

	for change in snapshot.changes:
		paths.update(_project_files(root, change.path))

		if change.status == ChangeStatus.DELETED:
			continue

		# A changed path that leads outside the repository through a link is
		# refused later by select_files, so its imports are never read.
		if _leads_outside(root, change.path):
			continue

		# The files this change imports directly.
		imports = find_imports(root, change.path)

		unresolved.update(f"{change.path}: {name}" for name in imports.unresolved)
		unreadable.update(imports.unreadable)

		for path in (*imports.paths, *_matching_tests(root, change.path)):
			if _leads_outside(root, path):
				outside.add(path)
			else:
				paths.add(path)

	return _ChangedContext(paths, unresolved, unreadable, outside)


def _leads_outside(root: Path, path: str) -> bool:
	"""Check whether a repository path resolves outside its root.

	Args:
		root: The repository root.
		path: A repository-relative path.

	Returns:
		True when the path leads outside the repository.
	"""
	return not (root / path).resolve().is_relative_to(root.resolve())


def _matching_tests(root: Path, changed_path: str) -> set[str]:
	"""Find same-name tests beside a file or in its project's test folders.

	Tests with the same name inside one project's test folders may belong to
	another source file; names alone cannot distinguish them.

	Args:
		root: The repository root.
		changed_path: The changed source file's repository-relative path.

	Returns:
		Matching test paths relative to the repository.
	"""
	# The changed file's base name, without its source suffix.
	stem = Path(changed_path).stem
	# Test names supported by Python, JavaScript and TypeScript projects.
	names = {
		f"test_{stem}.py",
		f"{stem}_test.py",
		*(
			f"{stem}.{kind}.{suffix[1:]}"
			for kind in ("test", "spec")
			for suffix in IMPORT_SOURCE_SUFFIXES
			if suffix != ".py"
		),
	}
	# The folder holding the changed file, where tests may sit beside it.
	source_folder = (root / changed_path).parent
	# The changed file's project, or the repository when it has no manifest.
	project_folder = _nearest_project_folder(root, changed_path) or root
	# The folders in that project that hold its tests.
	test_folders = (project_folder / "tests", project_folder / "__tests__")
	# Existing name matches within those folders.
	matches: set[str] = set()

	for folder in (source_folder, *test_folders):
		if not folder.is_dir():
			continue

		# Tests beside the source are direct children only.
		candidates = folder.iterdir() if folder == source_folder else folder.rglob("*")

		for path in candidates:
			if path.name in names and path.is_file() and not is_ignored(root, path):
				matches.add(repository_path(root, path))

	return matches


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

	return candidate.is_file() and not _leads_outside(root, path)


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
