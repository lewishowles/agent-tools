"""Record what a draft covers, so the build step can tell what changed since."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from review_packet.selection import leads_outside
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
	"""The hashes that show whether a selected file changed after drafting.

	A plain file has one hash of its contents. A link has a hash of its link
	text and, when it points inside the repository, a hash of the contents it
	points to, so build notices when the target changes.
	"""

	# The selected repository-relative path.
	path: str
	# The SHA-256 hash of the file's bytes, or of the link text for a symlink.
	sha256: str
	# The SHA-256 hash of the target's contents for a link inside the repository.
	target_sha256: str | None = None


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


def create(
	root: Path,
	snapshot: Snapshot,
	contents: tuple[str, ...],
	selected_bytes: dict[str, bytes] | None = None,
) -> Manifest:
	"""Record a draft's base, changes and file hashes, with one fingerprint over them.

	The build step compares the packet against this record to spot files that
	changed after the draft was written.

	Args:
		root: The repository root containing the selected files.
		snapshot: The resolved base and Git changes.
		contents: Every selected repository-relative file path.
		selected_bytes: File bytes already read by build, when available.

	Returns:
		The manifest, including its overall SHA-256 fingerprint.
	"""
	# Every change, including deleted paths that have no file to hash.
	changes = tuple(
		RecordedChange(change.path, change.status.value, change.old_path)
		for change in snapshot.changes
	)
	# The hash of each selected file.
	files = tuple(
		_file_hash(root, path, None if selected_bytes is None else selected_bytes[path])
		for path in contents
	)
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
		"files": [
			{
				"path": item.path,
				"sha256": item.sha256,
				**(
					{"target_sha256": item.target_sha256}
					if item.target_sha256 is not None
					else {}
				),
			}
			for item in files
		],
	}


def _file_hash(root: Path, path: str, content: bytes | None) -> FileHash:
	"""Hash a selected file and the target of an internal link.

	Args:
		root: The repository containing the selected file.
		path: The selected repository-relative path.
		content: Bytes already read from the file, when available.

	Returns:
		The file hash and an internal link's target content hash.
	"""
	# The selected path on disk.
	file_path = root / path

	if file_path.is_symlink():
		# The link text detects a changed destination without opening an outside file.
		link_hash = hashlib.sha256(os.fsencode(os.readlink(file_path))).hexdigest()
		if leads_outside(root, path):
			return FileHash(path, link_hash)

		# The target contents detect changes that leave the link text intact.
		target = content if content is not None else file_path.read_bytes()
		return FileHash(path, link_hash, hashlib.sha256(target).hexdigest())

	file_content = content if content is not None else file_path.read_bytes()
	return FileHash(path, hashlib.sha256(file_content).hexdigest())
