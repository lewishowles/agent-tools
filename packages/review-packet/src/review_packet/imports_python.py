"""Resolve direct Python imports within the changed file's project."""

import ast
import sys
from pathlib import Path

from review_packet.imports import ImportResult, repository_path


def python_imports(root: Path, file_path: Path) -> ImportResult:
	"""Find the files named by a Python file's relative and repository imports.

	Args:
		root: The repository root.
		file_path: The changed Python file.

	Returns:
		The imported files and the local imports with no file, or the changed
		file as unreadable when it can't be parsed.
	"""
	# Imported files found so far.
	found: set[str] = set()
	# Local imports with no matching repository file.
	missing: set[str] = set()

	try:
		# The parsed source of the changed file.
		tree = ast.parse(file_path.read_text(), filename=str(file_path))
	except (SyntaxError, UnicodeError, OSError):
		return ImportResult((), (), (repository_path(root, file_path),))

	# Folders that may hold the changed file's project packages.
	bases = _python_bases(root, file_path)

	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			# The files and missing names for one import statement.
			result = _plain_import(root, bases, node)
		elif isinstance(node, ast.ImportFrom):
			result = _from_import(root, file_path, bases, node)
		else:
			continue

		found.update(result.paths)
		missing.update(result.unresolved)

	return ImportResult(tuple(sorted(found)), tuple(sorted(missing)))


def _plain_import(
	root: Path, bases: tuple[Path, ...], node: ast.Import
) -> ImportResult:
	"""Find the repository files named by an `import a.b` statement.

	Args:
		root: The repository root.
		bases: Folders that may hold the repository's own packages.
		node: The import statement.

	Returns:
		The imported files, and the names that start with one of the
		repository's packages but match no file.
	"""
	# Imported files found so far.
	found: list[str] = []
	# Repository import names with no file.
	missing: list[str] = []

	for alias in node.names:
		if alias.name.split(".", 1)[0] in sys.stdlib_module_names:
			continue

		# The module's file, when the repository has it.
		match = _find_python_module(root, bases, alias.name)

		if match is not None:
			found.append(repository_path(root, match))
		elif _is_repository_python_name(bases, alias.name):
			missing.append(alias.name)

	return ImportResult(tuple(found), tuple(missing))


def _from_import(
	root: Path, file_path: Path, bases: tuple[Path, ...], node: ast.ImportFrom
) -> ImportResult:
	"""Find the repository files named by a `from … import …` statement.

	Args:
		root: The repository root.
		file_path: The changed Python file, which relative imports start from.
		bases: Folders that may hold the repository's own packages.
		node: The import statement.

	Returns:
		The imported module, any submodules imported from a package, and the
		local names that match no file.
	"""
	# Relative imports look only in the package their dots name.
	search_bases = (
		(_relative_python_base(file_path, node.level),) if node.level else bases
	)
	# The module before `import`, which is empty in `from . import a`.
	module = node.module or ""

	if not node.level and module.split(".", 1)[0] in sys.stdlib_module_names:
		return ImportResult((), ())

	if not module:
		# A missing member keeps the dots that identify its relative import.
		members = _package_members(root, search_bases[0], node.names)
		return ImportResult(
			members.paths,
			tuple("." * node.level + name for name in members.unresolved),
		)

	# The module's file, when the repository has it.
	match = _find_python_module(root, search_bases, module)

	if match is None:
		if node.level or _is_repository_python_name(search_bases, module):
			return ImportResult((), ("." * node.level + module,))

		return ImportResult((), ())

	# Submodules imported from a package, such as `from package import child`.
	children = (
		_package_members(root, match.parent, node.names).paths
		if match.name == "__init__.py"
		else ()
	)

	return ImportResult((repository_path(root, match), *children), ())


def _package_members(
	root: Path, package: Path, members: list[ast.alias]
) -> ImportResult:
	"""Find the files for names imported from a package folder.

	A name that isn't a submodule is defined in the package's `__init__.py`,
	so that file is used instead.

	Args:
		root: The repository root.
		package: The package folder.
		members: The imported names.

	Returns:
		The submodule or `__init__.py` file for each name, and the names
		that match neither.
	"""
	# Imported files found so far.
	found: list[str] = []
	# Names with no submodule and no package file to define them.
	missing: list[str] = []
	# The package's own file, which defines any name that isn't a submodule.
	package_file = package / "__init__.py"

	for member in members:
		if member.name == "*":
			continue

		# The submodule's file, or the package file that defines the name.
		match = _python_file(package / member.name) or (
			package_file if package_file.is_file() else None
		)

		if match is None or not match.is_relative_to(root):
			missing.append(member.name)
			continue

		found.append(repository_path(root, match))

	return ImportResult(tuple(found), tuple(missing))


def _python_bases(root: Path, file_path: Path) -> tuple[Path, ...]:
	"""Find the folders that a Python file's absolute imports are searched from.

	The search covers the repository root and its src folder, then every folder
	above the file that isn't a package, each with its own src folder.

	Args:
		root: The repository root.
		file_path: The changed Python file.

	Returns:
		The folders in the order they are tried.
	"""
	# The folders found so far, starting with the root and its src folder.
	bases = [root, root / "src"]

	for parent in file_path.parents:
		if not parent.is_relative_to(root):
			break

		# An absolute import does not start inside a package, so package folders
		# are skipped.
		if not (parent / "__init__.py").is_file():
			bases.extend((parent, parent / "src"))

	return tuple(dict.fromkeys(bases))


def _relative_python_base(file_path: Path, level: int) -> Path:
	"""Find the package folder named by a relative import.

	Args:
		file_path: The changed Python file.
		level: The number of leading dots in the import.

	Returns:
		The folder to search for the relative module.
	"""
	# The folder containing the changed file.
	base = file_path.parent

	for _ in range(level - 1):
		base = base.parent

	return base


def _find_python_module(root: Path, bases: tuple[Path, ...], name: str) -> Path | None:
	"""Find a Python module or package file inside the repository.

	Args:
		root: The repository root.
		bases: Folders that may contain the module.
		name: The dotted module name.

	Returns:
		The first matching file, or None.
	"""
	# Parts of the imported module name.
	parts = name.split(".")

	return next(
		(
			path
			for base in bases
			if (path := _python_file(base.joinpath(*parts))) is not None
			and path.is_relative_to(root)
		),
		None,
	)


def _is_repository_python_name(bases: tuple[Path, ...], name: str) -> bool:
	"""Decide whether a missing absolute name starts in this repository.

	Args:
		bases: Folders that may contain the module.
		name: The dotted module name.

	Returns:
		True when the repository has a Python module or a folder of Python files
		with the name's first part.
	"""
	# The first part of the dotted module name.
	first = name.split(".", 1)[0]

	return any(
		_python_file(base / first) is not None
		or ((base / first).is_dir() and any((base / first).rglob("*.py")))
		for base in bases
	)


def _python_file(path: Path) -> Path | None:
	"""Return a Python module or package entry file when present.

	Args:
		path: The module path without a suffix.

	Returns:
		The existing Python file, or None.
	"""
	# Possible module and package files.
	candidates = (path.with_suffix(".py"), path / "__init__.py")

	for candidate in candidates:
		if candidate.is_file():
			return candidate

	return None
