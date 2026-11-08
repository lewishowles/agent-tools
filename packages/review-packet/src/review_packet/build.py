"""Build an outside review archive from a current draft."""

import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from review_packet.manifest import create as create_manifest
from review_packet.manifest import to_json as manifest_to_json
from review_packet.scan import Finding, scan_content, scan_text
from review_packet.selection import OutsideSymlinkError, leads_outside
from review_packet.snapshot import (
	ChangeStatus,
	GitError,
	Snapshot,
	capture,
	repository_name,
)

# The first page tells the reviewer how to use the evidence in this archive.
START_HERE = """# Start here

Answer the main review question in REVIEW.md. Do not audit the whole project.

Treat verification.md as the author's reported results, not checks you ran yourself.
Treat deliberate omissions as limits on what you can conclude, not defects.
Repository files are under files/ at their repository-relative paths.
For findings, cite repository-relative path:line locations.
Say when the evidence is not enough to conclude.

Reply with a verdict, findings by severity, what you could not verify, and gaps
that limit your answer to the main question. End with the line requested in REVIEW.md.
"""


class StaleDraftError(ValueError):
	"""The selected files or Git changes no longer match the draft."""

	def __init__(self, paths: tuple[str, ...]) -> None:
		"""Keep the changed paths for the refusal message.

		Args:
			paths: Repository-relative paths that changed since draft.
		"""
		super().__init__("Draft is out of date. Run review-packet draft again.")
		self.paths = paths


class UnsafePacketError(ValueError):
	"""The archive would contain material the scanner refuses to share."""

	def __init__(self, findings: tuple[Finding, ...]) -> None:
		"""Keep locations and kinds without storing matched values.

		Args:
			findings: The scanner's safe location and kind records.
		"""
		super().__init__("Packet not written: secrets or local paths found.")
		self.findings = findings


def build_packet(
	root: Path, chunk_id: str, draft_folder: Path, output: Path | None
) -> Path:
	"""Check a draft and write its complete archive atomically.

	Args:
		root: The repository worktree to package.
		chunk_id: The chunk named in the draft folder and archive.
		draft_folder: The folder containing the three draft files.
		output: An explicit archive path, or None for Downloads.

	Returns:
		The completed archive path.

	Raises:
		StaleDraftError: The current files or change list differ from the draft.
		UnsafePacketError: The archive scan found private material.
		ValueError: The draft manifest is invalid.
		OSError: A draft or selected file cannot be read or the archive cannot be written.
	"""
	# The draft record controls which repository files enter the archive.
	manifest = _read_manifest(draft_folder / "manifest.json")
	# The checked repository paths recorded by draft.
	paths = tuple(item["path"] for item in manifest["files"])
	# The current Git snapshot, pinned to the draft's resolved base.
	snapshot = capture(root, manifest["base"])
	# Each selected file, read once so the staleness check, the scan and the zip
	# all use the same bytes.
	selected_bytes = _read_selected_files(root, paths)
	# All paths that changed after drafting, including a changed selection.
	stale = _stale_paths(root, snapshot, manifest, paths, selected_bytes)

	if stale:
		raise StaleDraftError(stale)

	# The authored documents stay exactly as written in the draft folder.
	brief = (draft_folder / "REVIEW.md").read_text()
	verification = (draft_folder / "verification.md").read_text()
	# One patch for files that existed at the base revision.
	diff = _changes_diff(root, snapshot)
	# Fixed documents and text prepared for the archive.
	text_entries = {
		"START-HERE.md": START_HERE,
		"REVIEW.md": brief,
		"verification.md": verification,
		"changes.diff": diff,
	}
	# Scan the same selected bytes that will be written into the archive.
	findings = list(scan_content(selected_bytes))

	for name, content in text_entries.items():
		findings.extend(scan_text(name, content))

	for name in paths:
		findings.extend(scan_text(f"files/{name}", ""))

	if findings:
		raise UnsafePacketError(tuple(findings))

	# The final destination is not touched until the zip is complete.
	destination = (
		output
		or Path.home() / "Downloads" / f"{repository_name(root)}-review-{chunk_id}.zip"
	)
	destination = destination.expanduser().absolute()
	destination.parent.mkdir(parents=True, exist_ok=True)
	# The temporary file shares the destination folder so replacement is atomic.
	with tempfile.NamedTemporaryFile(
		prefix=f".{destination.name}.",
		suffix=".tmp",
		dir=destination.parent,
		delete=False,
	) as temporary:
		# The partial zip, removed again if writing fails.
		temporary_path = Path(temporary.name)

	try:
		with zipfile.ZipFile(temporary_path, "w", zipfile.ZIP_DEFLATED) as archive:
			for name, content in text_entries.items():
				archive.writestr(name, content)

			for name, content in selected_bytes.items():
				archive.writestr(f"files/{name}", content)

		os.replace(temporary_path, destination)
	except BaseException:
		temporary_path.unlink(missing_ok=True)
		raise

	return destination


def _read_selected_files(root: Path, paths: tuple[str, ...]) -> dict[str, bytes]:
	"""Read each existing selected file once without following an outside link.

	Args:
		root: The repository containing the selected paths.
		paths: Repository-relative paths recorded in the draft.

	Returns:
		The bytes for each selected path that still exists.

	Raises:
		OutsideSymlinkError: A selected path leads outside the repository.
		OSError: An existing selected file cannot be read.
	"""
	# Selected file bytes in manifest order.
	selected: dict[str, bytes] = {}

	for name in paths:
		if not os.path.lexists(root / name):
			continue

		if leads_outside(root, name):
			raise OutsideSymlinkError(
				f"Path that leads outside the repository through a link can't be shared: {name}"
			)

		selected[name] = (root / name).read_bytes()

	return selected


def _read_manifest(path: Path) -> dict:
	"""Read and validate the paths needed to check and package a draft.

	Args:
		path: The manifest written beside the draft brief.

	Returns:
		The manifest fields in their stored JSON shape.

	Raises:
		ValueError: The manifest is malformed or names an unsafe path.
		OSError: The manifest cannot be read.
	"""
	try:
		# The parsed draft record, checked field by field below.
		manifest = json.loads(path.read_text())
		if not isinstance(manifest["files"], list) or not isinstance(
			manifest["changes"], list
		):
			raise ValueError
		if manifest["base"] is not None and not isinstance(manifest["base"], str):
			raise ValueError
		if not isinstance(manifest["fingerprint"], str):
			raise ValueError
		for item in manifest["files"]:
			# The selected file path, checked before it is opened.
			name = item["path"]
			if not isinstance(name, str) or not _safe_path(name):
				raise ValueError
			if not isinstance(item["sha256"], str):
				raise ValueError
			if "target_sha256" in item and not isinstance(item["target_sha256"], str):
				raise ValueError
		for item in manifest["changes"]:
			if not isinstance(item["path"], str) or not _safe_path(item["path"]):
				raise ValueError
			if item["old_path"] is not None and not _safe_path(item["old_path"]):
				raise ValueError
			if not isinstance(item["status"], str):
				raise ValueError
	except (KeyError, TypeError, ValueError) as error:
		raise ValueError(
			"Invalid draft manifest. Run review-packet draft again."
		) from error

	return manifest


def _safe_path(name: str) -> bool:
	"""Keep manifest paths inside the repository when opening files.

	Args:
		name: A path read from manifest.json.

	Returns:
		Whether the path is a repository-relative file name.
	"""
	# The manifest path split into parts for the checks.
	path = Path(name)

	return bool(name) and not path.is_absolute() and ".." not in path.parts


def _stale_paths(
	root: Path,
	snapshot: Snapshot,
	manifest: dict,
	paths: tuple[str, ...],
	selected_bytes: dict[str, bytes],
) -> tuple[str, ...]:
	"""Name files whose current content or change record differs from draft.

	Args:
		root: The repository containing selected files.
		snapshot: The current snapshot from the draft base.
		manifest: The recorded JSON fields.
		paths: The recorded selected paths.
		selected_bytes: The file bytes that will be scanned and archived.

	Returns:
		Sorted repository-relative paths that need a fresh draft, or
		manifest.json when only the base or the recorded fingerprint differs.
	"""
	# Missing paths cannot be hashed, so report them along with other changes.
	missing = set(paths) - selected_bytes.keys()
	# Hash the remaining files with the same function draft uses.
	current = create_manifest(
		root,
		snapshot,
		tuple(name for name in paths if name not in missing),
		selected_bytes,
	)
	# The file hashes recorded before checks ran during draft.
	before = {item["path"]: item for item in manifest["files"]}
	# Every selected path whose bytes, link text or link target changed.
	stale = missing | {
		item.path
		for item in current.files
		if item.sha256 != before[item.path]["sha256"]
		or item.target_sha256 != before[item.path].get("target_sha256")
	}
	# Change list entries that were added, removed or changed.
	old_changes = {item["path"]: item for item in manifest["changes"]}
	new_changes = {item["path"]: item for item in manifest_to_json(current)["changes"]}

	for name in old_changes.keys() | new_changes.keys():
		if old_changes.get(name) != new_changes.get(name):
			stale.add(name)

	# A base move or a hand-edited fingerprint has no matching changed file.
	if not stale and current.fingerprint != manifest["fingerprint"]:
		stale.add("manifest.json")

	return tuple(sorted(stale))


def _changes_diff(root: Path, snapshot: Snapshot) -> str:
	"""Return one patch for changed paths that existed at the base.

	Args:
		root: The repository containing the changed files.
		snapshot: The checked Git snapshot.

	Returns:
		A combined patch, or empty text before the first commit.

	Raises:
		GitError: Git could not produce the patch.
	"""
	if snapshot.base_sha is None:
		return ""

	# New files are already present in full and do not need patch entries.
	paths = sorted(
		{
			name
			for change in snapshot.changes
			if change.status not in (ChangeStatus.ADDED, ChangeStatus.UNTRACKED)
			for name in (change.path, change.old_path)
			if name is not None
		}
	)

	if not paths:
		return ""

	# Git's patch output, or its error text.
	result = subprocess.run(
		[
			"git",
			"diff",
			"--binary",
			"--no-ext-diff",
			"--no-color",
			"--find-renames",
			snapshot.base_sha,
			"--",
			*paths,
		],
		cwd=root,
		capture_output=True,
		check=False,
	)

	if result.returncode:
		raise GitError(
			result.stderr.decode(errors="replace").strip() or "Git diff failed."
		)

	return result.stdout.decode(errors="replace")
