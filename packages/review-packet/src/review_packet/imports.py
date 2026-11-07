"""Find the files a changed source file imports, and the files that import it."""

from dataclasses import dataclass
from pathlib import Path

from review_packet.snapshot import Change, ChangeStatus, repository_files

# The suffixes of files whose imports are read.
IMPORT_SOURCE_SUFFIXES = (
	".py",
	".js",
	".jsx",
	".ts",
	".tsx",
	".mjs",
	".mts",
	".cjs",
	".cts",
)

# File suffixes an import may resolve to.
RESOLVED_SOURCE_SUFFIXES = (*IMPORT_SOURCE_SUFFIXES, ".json")


@dataclass(frozen=True)
class ImportResult:
	"""The files a source file imports, and the imports that could not be followed."""

	# Repository-relative paths of the imported files.
	paths: tuple[str, ...]
	# Local import names that match no file in the repository.
	unresolved: tuple[str, ...]
	# Repository-relative paths of source files whose text could not be read or parsed.
	unreadable: tuple[str, ...] = ()


def repository_path(root: Path, path: Path) -> str:
	"""Give a file's path relative to the repository, with forward slashes.

	Args:
		root: The repository root.
		path: A file inside the repository.

	Returns:
		The repository-relative path.
	"""
	return path.relative_to(root).as_posix()


def find_imports(root: Path, source: str) -> ImportResult:
	"""Find the repository files that one source file imports directly.

	Only Python, JavaScript and TypeScript files are read. Imports of outside
	packages are ignored, and imported files are not followed further. Python
	imports from sibling packages in this repository are reported as unresolved.
	JavaScript and TypeScript imports by package name count as outside packages,
	so they are ignored even when the package is in this repository.

	Args:
		root: The repository root.
		source: The changed file's repository-relative path.

	Returns:
		The imported files, the local imports with no file, and the source file
		itself when its text could not be read.
	"""
	return _find_imports(root, source, frozenset())


def find_callers(root: Path, changes: tuple[Change, ...]) -> tuple[str, ...]:
	"""List the files that directly import a changed file from the same package.

	A file that imports a deleted file, or a renamed file by its old path, still
	counts, so the reviewer sees what the change breaks. Importers in other
	packages of this repository are not listed.

	Args:
		root: The repository root.
		changes: The changed files, including deletions and renames.

	Returns:
		Repository-relative paths of the importing files, sorted. A changed
		file that imports another changed file is included.

	Raises:
		GitError: Git could not list the repository's files.
	"""
	# Changed files grouped by the nearest package manifest.
	targets: dict[Path, set[str]] = {}

	for change in changes:
		for path in (change.path, change.old_path):
			if path is not None:
				targets.setdefault(_package_root(root, path), set()).add(path)

	# Files gone from disk that an import should still match.
	deleted_paths = _deleted_paths(root, changes)
	# The files found to import a changed file.
	callers: set[str] = set()

	for name in repository_files(root):
		if Path(name).suffix not in IMPORT_SOURCE_SUFFIXES:
			continue

		# The possible caller on disk.
		file_path = root / name

		# Skip deleted files and links that lead outside the repository.
		if not file_path.is_file() or not file_path.resolve().is_relative_to(
			root.resolve()
		):
			continue

		# Changed files in the caller's package, other than the caller itself.
		package_targets = targets.get(_package_root(root, name), set()) - {name}

		if package_targets and package_targets.intersection(
			_find_imports(root, name, deleted_paths).paths
		):
			callers.add(name)

	return tuple(sorted(callers))


def _deleted_paths(root: Path, changes: tuple[Change, ...]) -> frozenset[Path]:
	"""List the paths a change removed: deleted files and renamed files' old names.

	Args:
		root: The repository root.
		changes: The changed files.

	Returns:
		The absolute paths of deleted files and of renamed files' old names.
	"""
	return frozenset(
		root / path
		for change in changes
		for path in (
			change.old_path,
			change.path if change.status == ChangeStatus.DELETED else None,
		)
		if path is not None
	)


def _package_root(root: Path, path: str) -> Path:
	"""Find the folder of the package that holds a repository path.

	A package is the nearest folder above the path with a `pyproject.toml` or
	`package.json` file.

	Args:
		root: The repository root.
		path: A repository-relative path.

	Returns:
		The package folder, or the repository root when no package holds the path.
	"""
	# The folder being checked for a package file, starting next to the path.
	folder = (root / path).parent

	while folder.is_relative_to(root):
		if (folder / "pyproject.toml").is_file() or (folder / "package.json").is_file():
			return folder

		if folder == root:
			break

		folder = folder.parent

	return root


def _find_imports(
	root: Path, source: str, deleted_paths: frozenset[Path]
) -> ImportResult:
	"""Find the repository files that one source file imports directly.

	Args:
		root: The repository root.
		source: The source file's repository-relative path.
		deleted_paths: Deleted files to resolve while finding callers. Empty
			when following imports into the packet.

	Returns:
		The imported files, the local imports with no file, and the source file
		itself when its text could not be read.
	"""
	# The source file on disk.
	file_path = root / source

	if not file_path.is_file() or file_path.suffix not in IMPORT_SOURCE_SUFFIXES:
		return ImportResult((), ())

	# The language modules are imported here because they import ImportResult
	# from this file.
	if file_path.suffix == ".py":
		from review_packet.imports_python import python_imports

		return python_imports(root, file_path, deleted_paths)

	from review_packet.imports_javascript import javascript_imports

	return javascript_imports(root, file_path, deleted_paths)
