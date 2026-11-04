"""Record what a draft covers, so the build step can tell what changed since."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from review_packet.snapshot import Snapshot


@dataclass(frozen=True)
class RecordedChange:
	"""A Git change included in the draft selection."""

	# The current repository-relative path.
	path: str
	# How the path differs from the base, such as "added" or "deleted".
	status: str
	# The previous path for a rename or copy.
	old_path: str | None


@dataclass(frozen=True)
class FileHash:
	"""A selected file and the hash of its contents or link text."""

	# The selected repository-relative path.
	path: str
	# The SHA-256 hash of the file's bytes, or of the link text for a symlink.
	sha256: str


@dataclass(frozen=True)
class Manifest:
	"""The base commit, changes and file hashes that the build step checks."""

	# The resolved Git base commit, or None before the first commit.
	base: str | None
	# The Git changes since the base.
	changes: tuple[RecordedChange, ...]
	# One hash for each file selected for the packet.
	files: tuple[FileHash, ...]
	# One hash over the other fields, so a single value shows whether anything changed.
	fingerprint: str


def create(root: Path, snapshot: Snapshot, contents: tuple[str, ...]) -> Manifest:
	"""Record a draft's base, changes and file hashes, with one fingerprint over them.

	The build step compares the packet against this record to spot files that
	changed after the draft was written.

	Args:
		root: The repository root containing the selected files.
		snapshot: The resolved base and Git changes.
		contents: Every selected repository-relative file path.

	Returns:
		The manifest, including its overall SHA-256 fingerprint.
	"""
	# Every change, including deleted paths that have no file to hash.
	changes = tuple(
		RecordedChange(change.path, change.status.value, change.old_path)
		for change in snapshot.changes
	)
	# The hash of each selected file.
	files = tuple(FileHash(path, _file_hash(root / path)) for path in contents)
	# The record as compact JSON with sorted keys, so the same record always gives
	# the same fingerprint.
	content = json.dumps(
		_json_fields(snapshot.base_sha, changes, files),
		sort_keys=True,
		separators=(",", ":"),
	)

	return Manifest(
		snapshot.base_sha,
		changes,
		files,
		hashlib.sha256(content.encode()).hexdigest(),
	)


def to_json(manifest: Manifest) -> dict:
	"""Return the fields written to the draft's manifest.json file.

	Args:
		manifest: The completed draft manifest.

	Returns:
		The manifest fields in a JSON-ready mapping, with the fingerprint last.
	"""
	return {
		**_json_fields(manifest.base, manifest.changes, manifest.files),
		"fingerprint": manifest.fingerprint,
	}


def _json_fields(
	base: str | None, changes: tuple[RecordedChange, ...], files: tuple[FileHash, ...]
) -> dict:
	"""Return the fields included in the manifest fingerprint.

	Args:
		base: The resolved base commit, if one exists.
		changes: The Git changes included in the selection.
		files: The selected files and their hashes.

	Returns:
		The JSON-ready fields before the fingerprint is added.
	"""
	return {
		"base": base,
		"changes": [
			{"path": change.path, "status": change.status, "old_path": change.old_path}
			for change in changes
		],
		"files": [{"path": item.path, "sha256": item.sha256} for item in files],
	}


def _file_hash(file_path: Path) -> str:
	"""Hash a selected file's bytes, or a symlink's own link text.

	Args:
		file_path: The selected path, which must exist inside the repository.

	Returns:
		The hexadecimal SHA-256 hash of the file or link text.
	"""
	# A link's own text is hashed, so a link never leads to reading a file outside
	# the repository.
	content = (
		os.fsencode(os.readlink(file_path))
		if file_path.is_symlink()
		else file_path.read_bytes()
	)

	return hashlib.sha256(content).hexdigest()
