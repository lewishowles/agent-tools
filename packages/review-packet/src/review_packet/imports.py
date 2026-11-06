"""Find repository files directly imported by a changed source file."""

from dataclasses import dataclass
from pathlib import Path

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
	# The changed source file on disk.
	file_path = root / source

	if not file_path.is_file() or file_path.suffix not in IMPORT_SOURCE_SUFFIXES:
		return ImportResult((), ())

	# The language modules are imported here because they import ImportResult
	# from this file.
	if file_path.suffix == ".py":
		from review_packet.imports_python import python_imports

		return python_imports(root, file_path)

	from review_packet.imports_javascript import javascript_imports

	return javascript_imports(root, file_path)
