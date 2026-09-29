"""Store and validate review-feedback drafts outside the worktree."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from review_feedback.git_selection import (
	GitCommandError,
	SelectionLocation,
	_coordinates,
	_find_matches,
	_resolve_worktree,
	_run_git,
)


class DraftError(RuntimeError):
	"""Base error for active-draft failures."""


@dataclass(frozen=True)
class ValidationNotice:
	"""One entry's validation outcome: an ambiguous relocation or a missing selection."""

	entry_number: int
	message: str
	candidate_locations: tuple[str, ...] = ()
	missing: bool = False


@dataclass(frozen=True)
class ValidationReport:
	"""Validation notices for a draft, alongside the repository fingerprint checked against."""

	current_fingerprint: str
	notices: tuple[ValidationNotice, ...] = ()

	@property
	def missing(self) -> tuple[ValidationNotice, ...]:
		"""Return only the notices for selections that no longer exist anywhere in their file."""
		return tuple(notice for notice in self.notices if notice.missing)


class NoActiveDraftError(DraftError):
	"""Raised when a command needs an active draft but none exists."""


class DraftValidationError(DraftError):
	"""Raised when one or more stored selections no longer exist anywhere in their file."""

	def __init__(self, notices: Sequence[ValidationNotice]) -> None:
		if not notices:
			raise ValueError("at least one validation notice is required")

		self.entry_numbers = tuple(notice.entry_number for notice in notices)
		super().__init__(
			"\n".join(
				f"entry {notice.entry_number} is stale: "
				f"{validation_notice_text(notice)}; remove it with "
				f"`review-feedback remove {notice.entry_number}` and capture it again"
				for notice in notices
			)
		)


@dataclass(frozen=True)
class DraftEntry:
	"""Persisted location and comment for one review entry."""

	number: int
	path: str
	side: str
	start_line: int
	start_column: int
	end_line: int
	end_column: int
	selection_hash: str
	repository_fingerprint: str
	comment: str
	selection_text: str = ""
	old_path: str | None = None
	new_path: str | None = None


def location_text(entry: DraftEntry) -> str:
	"""Format a persisted repository-relative location."""
	return (
		f"{entry.path}:{entry.start_line}:{entry.start_column}-"
		f"{entry.end_line}:{entry.end_column}"
	)


@dataclass
class Draft:
	"""Active review entries and the next stable entry number."""

	entries: list[DraftEntry]
	next_entry_number: int = 1


@dataclass(frozen=True)
class DraftStore:
	"""Repository root and worktree-specific active-draft path."""

	root: Path
	active_path: Path


def resolve_store(cwd: Path | str | None = None) -> DraftStore:
	"""Resolve the active-draft path without creating repository state."""
	root = _resolve_worktree(cwd)

	try:
		result = _run_git(root, ["rev-parse", "--git-path", "review-feedback"])
	except GitCommandError as error:
		raise DraftError(f"could not resolve Git metadata path: {error}") from error

	metadata_path = Path(os.fsdecode(result.stdout).strip())
	if not metadata_path.is_absolute():
		metadata_path = root / metadata_path

	return DraftStore(root, metadata_path / "active.json")


def load_active(store: DraftStore) -> Draft | None:
	"""Load the active draft, or return None when no draft exists."""
	if not store.active_path.exists():
		return None

	try:
		data = json.loads(store.active_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as error:
		raise DraftError(f"could not read active draft: {error}") from error

	if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
		raise DraftError("active draft has an invalid format")

	try:
		entries = [_entry_from_data(entry) for entry in data["entries"]]
		next_entry_number = int(data.get("next_entry_number", 1))
	except (TypeError, ValueError, KeyError) as error:
		raise DraftError("active draft has an invalid entry") from error

	if next_entry_number < 1:
		raise DraftError("active draft has an invalid next entry number")

	return Draft(entries, next_entry_number)


def save_active(store: DraftStore, draft: Draft) -> None:
	"""Atomically save the active draft under Git metadata."""
	store.active_path.parent.mkdir(parents=True, exist_ok=True)
	temporary_path = store.active_path.with_name(
		f".{store.active_path.name}.{uuid.uuid4().hex}.tmp"
	)

	data = {
		"version": 1,
		"next_entry_number": draft.next_entry_number,
		"entries": [asdict(entry) for entry in draft.entries],
	}

	try:
		temporary_path.write_text(
			json.dumps(data, indent=2, sort_keys=True) + "\n",
			encoding="utf-8",
		)
		temporary_path.replace(store.active_path)
	except OSError as error:
		temporary_path.unlink(missing_ok=True)
		raise DraftError(f"could not save active draft: {error}") from error


def append_entry(
	draft: Draft,
	location: SelectionLocation,
	selection: str,
	comment: str,
	repository_fingerprint: str,
) -> DraftEntry:
	"""Append a selection to a draft and return its stable numbered entry."""
	entry = DraftEntry(
		number=draft.next_entry_number,
		path=location.path,
		side=location.side,
		start_line=location.start_line,
		start_column=location.start_column,
		end_line=location.end_line,
		end_column=location.end_column,
		selection_hash=hash_selection(selection),
		repository_fingerprint=repository_fingerprint,
		comment=comment,
		selection_text=selection,
		old_path=location.old_path,
		new_path=location.new_path,
	)
	draft.entries.append(entry)
	draft.next_entry_number += 1
	return entry


def remove_entry(draft: Draft, number: int) -> DraftEntry:
	"""Remove one entry while preserving every remaining entry number."""
	for index, entry in enumerate(draft.entries):
		if entry.number == number:
			return draft.entries.pop(index)

	raise DraftError(f"no active entry numbered {number}")


def hash_selection(selection: str) -> str:
	"""Return a SHA-256 digest of the given selection text, for cheap equality checks."""
	return hashlib.sha256(selection.encode("utf-8")).hexdigest()


def repository_fingerprint(root: Path) -> str:
	"""Return a digest of the Git state and current untracked file contents."""
	digest = hashlib.sha256()

	head = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"], check=False)
	digest.update(b"head\0")
	digest.update(head.stdout if head.returncode == 0 else b"<no-head>")

	status = _run_git(
		root,
		["status", "--porcelain=v1", "-z", "--untracked-files=all"],
	).stdout
	digest.update(b"status\0")
	digest.update(status)

	if head.returncode == 0:
		changed = _run_git(root, ["diff", "--binary", "HEAD", "--"]).stdout
		digest.update(b"diff\0")
		digest.update(changed)
	else:
		tracked_paths = _run_git(root, ["ls-files", "--cached", "-z"]).stdout
		digest.update(b"tracked\0")
		digest.update(tracked_paths)
		for path in _split_paths(tracked_paths):
			digest.update(_current_file_fingerprint(root, path))

	untracked_paths = _run_git(
		root,
		["ls-files", "--others", "--exclude-standard", "-z"],
	).stdout
	digest.update(b"untracked\0")
	for path in sorted(_split_paths(untracked_paths)):
		digest.update(path)
		digest.update(b"\0")
		digest.update(_current_file_fingerprint(root, path))

	return digest.hexdigest()


def validate_entries(
	store: DraftStore,
	draft: Draft,
	*,
	strict: bool = True,
) -> ValidationReport:
	"""Validate every entry, relocating unique matches in the draft in place.

	Returns a report of every entry needing attention (ambiguous or missing).
	When strict, raises DraftValidationError if any entry is genuinely missing.
	"""
	current_fingerprint = repository_fingerprint(store.root)
	notices: list[ValidationNotice] = []

	for index, entry in enumerate(draft.entries):
		relocated_entry, notice = _validate_entry(store.root, entry)
		if relocated_entry is not None:
			draft.entries[index] = relocated_entry
		if notice is not None:
			notices.append(notice)

	report = ValidationReport(current_fingerprint, tuple(notices))
	if strict and report.missing:
		raise DraftValidationError(report.missing)

	return report


def retire_active(store: DraftStore, reason: str) -> Path:
	"""Move an active draft to a recoverable task-specific Trash path."""
	if not store.active_path.exists():
		raise NoActiveDraftError("no active review draft exists")

	trash_path = _trash_root() / _trash_filename(store.root, reason)
	trash_path.parent.mkdir(parents=True, exist_ok=True)

	try:
		shutil.move(str(store.active_path), str(trash_path))
	except OSError as error:
		raise DraftError(
			f"could not move the active draft to Trash: {error}"
		) from error

	return trash_path


def _entry_from_data(data: object) -> DraftEntry:
	"""Decode one JSON entry into the persisted draft shape."""
	if not isinstance(data, dict):
		raise TypeError("entry is not an object")

	return DraftEntry(
		number=int(data["number"]),
		path=str(data["path"]),
		side=str(data["side"]),
		start_line=int(data["start_line"]),
		start_column=int(data["start_column"]),
		end_line=int(data["end_line"]),
		end_column=int(data["end_column"]),
		selection_hash=str(data["selection_hash"]),
		repository_fingerprint=str(data["repository_fingerprint"]),
		comment=str(data["comment"]),
		selection_text=str(data.get("selection_text", "")),
		old_path=(str(data["old_path"]) if data.get("old_path") is not None else None),
		new_path=(str(data["new_path"]) if data.get("new_path") is not None else None),
	)


def _split_paths(output: bytes) -> list[bytes]:
	"""Split NUL-delimited Git paths."""
	return [path for path in output.split(b"\0") if path]


def _current_file_fingerprint(root: Path, path: bytes) -> bytes:
	"""Return a stable marker and bytes for a current worktree path."""
	decoded_path = os.fsdecode(path)
	file_path = root / decoded_path

	if file_path.is_symlink():
		return b"<symlink>"

	try:
		return file_path.read_bytes()
	except OSError:
		return b"<missing>"


def _validate_entry(
	root: Path, entry: DraftEntry
) -> tuple[DraftEntry | None, ValidationNotice | None]:
	"""Check one entry against its current file, relocating a single shifted match.

	Returns (relocated_entry, None) when the stored text moved to exactly one
	new location, (None, notice) when it's ambiguous or missing, or (None, None)
	when the cached location is still correct.
	"""
	content = _read_entry_content(root, entry)
	if content is None:
		return None, ValidationNotice(
			entry_number=entry.number,
			message=f"`{entry.path}` is missing or unreadable",
			missing=True,
		)

	start = _offset_for_location(content, entry.start_line, entry.start_column)
	end = _offset_for_location(content, entry.end_line, entry.end_column)
	if (
		start is not None
		and end is not None
		and end >= start
		and hash_selection(content[start:end]) == entry.selection_hash
	):
		return None, None

	positions = (
		_find_matches(entry.selection_text, content) if entry.selection_text else []
	)
	if len(positions) == 1:
		coordinates = _coordinates(content, positions[0], len(entry.selection_text))
		return replace(entry, **coordinates), None

	if len(positions) > 1:
		candidate_locations = tuple(
			location_text(
				replace(
					entry, **_coordinates(content, position, len(entry.selection_text))
				)
			)
			for position in positions
		)
		return None, ValidationNotice(
			entry_number=entry.number,
			message=f"`{entry.path}` contains the stored selection at multiple locations",
			candidate_locations=candidate_locations,
		)

	return None, ValidationNotice(
		entry_number=entry.number,
		message=f"`{entry.path}` no longer contains the stored selection",
		missing=True,
	)


def validation_notice_text(notice: ValidationNotice) -> str:
	"""Format a notice's message, appending candidate locations for an ambiguous match."""
	if not notice.candidate_locations:
		return notice.message

	locations = ", ".join(notice.candidate_locations)
	return f"{notice.message}; candidates: {locations}"


def _read_entry_content(root: Path, entry: DraftEntry) -> str | None:
	"""Read the current or HEAD text addressed by a stored entry."""
	if entry.side == "removed":
		try:
			data = _run_git(root, ["show", f"HEAD:{entry.path}"]).stdout
		except GitCommandError:
			return None
	else:
		file_path = root / entry.path
		if file_path.is_symlink():
			return None

		try:
			data = file_path.read_bytes()
		except OSError:
			return None

	if b"\0" in data:
		return None

	try:
		return data.decode("utf-8")
	except UnicodeDecodeError:
		return None


def _offset_for_location(content: str, line: int, column: int) -> int | None:
	"""Convert one-based line and column coordinates to a character offset."""
	if line < 1 or column < 1:
		return None

	line_starts = [0]
	line_starts.extend(
		index + 1 for index, character in enumerate(content) if character == "\n"
	)
	if line > len(line_starts):
		return None

	line_start = line_starts[line - 1]
	offset = line_start + column - 1
	line_end = line_starts[line] - 1 if line < len(line_starts) else len(content)
	if offset > line_end:
		return None

	return offset


def _trash_root() -> Path:
	"""Return the task-specific macOS Trash directory."""
	return Path.home() / ".Trash" / "review-feedback"


def _trash_filename(root: Path, reason: str) -> str:
	"""Return a unique recoverable filename for a retired draft."""
	timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	repository_name = root.name or "repository"
	return f"{repository_name}-{reason}-{timestamp}-{uuid.uuid4().hex}.json"
