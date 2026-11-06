"""Resolve direct JavaScript and TypeScript imports."""

import json
import os
import re
from pathlib import Path

from review_packet.imports import (
	ImportResult,
	RESOLVED_SOURCE_SUFFIXES,
	repository_path,
)

# Matches the quoted source of an import, export or require. Import and export
# declarations start at the beginning of a line. The list of names before `from`
# may span lines but cannot include another quoted source.
JS_IMPORT = re.compile(
	r"(?:^[ \t]*(?:import|export)\s+(?:[\w$\s{},*]+?\s+from\s+)?"
	r"|\brequire\s*\(\s*)['\"]([^'\"]+)['\"]",
	re.MULTILINE,
)

# The file types an import may bring into the packet. Other files, such as
# images and fonts, are left out without a note.
PACKET_SUFFIXES = (*RESOLVED_SOURCE_SUFFIXES, ".vue", ".css")


def javascript_imports(root: Path, file_path: Path) -> ImportResult:
	"""Resolve JavaScript and TypeScript relative imports and package aliases.

	Args:
		root: The repository root.
		file_path: The changed source file.

	Returns:
		The imported files, missing names, and unreadable files.
	"""
	# Repository files found from direct imports.
	found: set[str] = set()
	# Local import names without files.
	missing: set[str] = set()

	try:
		# The changed file's source text.
		source = file_path.read_text()
	except (UnicodeError, OSError):
		return ImportResult((), (), (repository_path(root, file_path),))

	for name in JS_IMPORT.findall(source):
		# Where the import points before a suffix or index file is added.
		if name.startswith("."):
			candidate = file_path.parent / name
		elif name.startswith("#"):
			candidate = _alias_target(root, file_path, name)
		else:
			continue

		if candidate is None:
			missing.add(name)
			continue

		candidate = Path(os.path.normpath(candidate))

		if not candidate.is_relative_to(root):
			missing.add(name)
			continue

		# Existing files of other types, such as images or fonts, stay out of the packet.
		if candidate.is_file() and candidate.suffix not in PACKET_SUFFIXES:
			continue

		# The imported file, when it exists.
		match = _source_file(candidate)

		if match is None or not match.is_relative_to(root):
			missing.add(name)
			continue

		found.add(repository_path(root, match))

	return ImportResult(tuple(sorted(found)), tuple(sorted(missing)))


def _alias_target(root: Path, file_path: Path, name: str) -> Path | None:
	"""Find the file a # import alias points to, using the nearest package.json.

	Args:
		root: The repository root.
		file_path: The changed source file.
		name: The imported alias.

	Returns:
		The alias target, or None when it cannot be read or matched.
	"""
	for folder in file_path.parents:
		if not folder.is_relative_to(root):
			break

		# The package file that may declare the alias.
		manifest = folder / "package.json"

		if not manifest.is_file():
			continue

		try:
			# The package manifest data.
			data = json.loads(manifest.read_text())
		except (OSError, UnicodeError, json.JSONDecodeError):
			return None

		if not isinstance(data, dict):
			return None

		# The aliases declared by the nearest package.
		imports = data.get("imports", {})

		if not isinstance(imports, dict):
			return None

		for pattern, target in imports.items():
			if not isinstance(target, str):
				continue

			if pattern == name:
				return folder / target

			if pattern.endswith("*") and name.startswith(pattern[:-1]):
				return folder / target.replace("*", name[len(pattern) - 1 :])

		return None

	return None


def _source_file(path: Path) -> Path | None:
	"""Return an existing source file or directory index file.

	Args:
		path: The import target before suffix lookup.

	Returns:
		The existing file, or None.
	"""
	# The files to try, in order: the target itself, a suffix added (./user.service),
	# a suffix replaced (./foo.js for foo.ts), and a folder's index file.
	candidates = [path]
	candidates.extend(Path(f"{path}{suffix}") for suffix in RESOLVED_SOURCE_SUFFIXES)
	candidates.extend(path.with_suffix(suffix) for suffix in RESOLVED_SOURCE_SUFFIXES)
	candidates.extend(path / f"index{suffix}" for suffix in RESOLVED_SOURCE_SUFFIXES)

	for candidate in candidates:
		if candidate.is_file():
			return candidate

	return None
