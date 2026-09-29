"""Resolve a copied text selection to its exact location in the current Git worktree."""

from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import subprocess


class SelectionError(RuntimeError):
	"""Base error for all review-feedback selection failures."""

	pass


class GitWorktreeError(SelectionError):
	"""Raised when the command is not run inside a Git worktree."""

	pass


class GitCommandError(SelectionError):
	"""Raised when a Git subprocess call fails or Git is unavailable."""

	pass


class EmptySelectionError(SelectionError):
	"""Raised when the selection text is empty."""

	pass


class SelectionNotFoundError(SelectionError):
	"""Raised when a selection matches no location."""

	pass


class SelectionSpansSidesError(SelectionError):
	"""Raised when a selection combines removed and current content from the same diff."""

	pass


@dataclass(frozen=True)
class SelectionLocation:
	"""Where a selection resolved to, and which diff side it came from.

	`side` is "current" for added or unchanged content, or "removed" for
	content that exists only in HEAD. `old_path`/`new_path` are set only for
	renames, on both sides, so a removed location still carries its current
	name. Columns are one-based and end-exclusive.
	"""

	path: str
	side: str
	start_line: int
	start_column: int
	end_line: int
	end_column: int
	old_path: str | None = None
	new_path: str | None = None


@dataclass(frozen=True)
class _ChangedPath:
	"""A tracked or untracked path's current and HEAD locations, if any."""

	current_path: str | None
	head_path: str | None
	old_path: str | None = None
	new_path: str | None = None


@dataclass(frozen=True)
class _FileContents:
	"""A changed path paired with its decoded current and HEAD text."""

	changed_path: _ChangedPath
	current: str | None
	head: str | None


def resolve_selection(
	selection: str, cwd: Path | str | None = None
) -> list[SelectionLocation]:
	"""Resolve `selection` to every location it matches in the worktree at `cwd`.

	Current-side content is checked before removed (HEAD-only) content for
	each file, so unchanged context common to both sides always resolves to
	its current location. Raises EmptySelectionError, SelectionNotFoundError,
	or SelectionSpansSidesError; see each error class for the failure it reports.
	"""
	if selection == "":
		raise EmptySelectionError("clipboard selection is empty")

	root = _resolve_worktree(cwd)
	file_contents = _load_changed_files(root)
	matches: list[SelectionLocation] = []

	for contents in file_contents:
		current_matches = _find_matches(selection, contents.current)
		if current_matches:
			matches.extend(
				_make_locations(
					selection,
					current_matches,
					contents.changed_path,
					contents.current,
					"current",
				)
			)
			continue

		head_matches = _find_matches(selection, contents.head)
		if head_matches:
			matches.extend(
				_make_locations(
					selection,
					head_matches,
					contents.changed_path,
					contents.head,
					"removed",
				)
			)

	if matches:
		return matches

	if _selection_spans_sides(selection, file_contents):
		raise SelectionSpansSidesError(
			"selection combines removed and current content; select one diff side"
		)

	raise SelectionNotFoundError("selection was not found in changed files")


def _resolve_worktree(cwd: Path | str | None) -> Path:
	"""Return the absolute root of the Git worktree containing `cwd`."""
	working_directory = Path.cwd() if cwd is None else Path(cwd)

	try:
		result = _run_git(
			working_directory,
			["rev-parse", "--show-toplevel"],
		)
	except GitCommandError as error:
		raise GitWorktreeError(
			"current directory is not inside a Git worktree"
		) from error

	root_text = result.stdout.decode().strip()
	if not root_text:
		raise GitWorktreeError("Git returned an empty worktree path")

	return Path(root_text).resolve()


def _run_git(
	root: Path,
	arguments: list[str],
	check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
	"""Run a Git subprocess and raise GitCommandError on failure or when Git is missing."""
	try:
		result = subprocess.run(
			["git", *arguments],
			cwd=root,
			capture_output=True,
			check=False,
		)
	except FileNotFoundError as error:
		raise GitCommandError("git was not found on PATH") from error
	except OSError as error:
		raise GitCommandError(f"git command failed: {error}") from error

	if check and result.returncode != 0:
		detail = result.stderr.decode(errors="replace").strip()
		message = "git command failed"
		if detail:
			message = f"{message}: {detail}"

		raise GitCommandError(message)

	return result


def _load_changed_files(root: Path) -> list[_FileContents]:
	"""Read current and HEAD content for every changed tracked path, plus untracked text files not already covered."""
	has_head = _has_head(root)
	changed_paths = _changed_paths(root, has_head)
	known_current_paths = {
		changed_path.current_path
		for changed_path in changed_paths
		if changed_path.current_path is not None
	}

	untracked_paths = _untracked_paths(root)

	changed_paths.extend(
		_changed_path_for_untracked(path)
		for path in untracked_paths
		if path not in known_current_paths
	)

	return [_file_contents(root, changed_path) for changed_path in changed_paths]


def _has_head(root: Path) -> bool:
	"""Return whether the worktree has a HEAD commit."""
	result = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"], check=False)
	return result.returncode == 0


def _changed_paths(root: Path, has_head: bool) -> list[_ChangedPath]:
	"""List every changed tracked path, or every tracked path if the worktree has no HEAD yet.

	Added (A) files have no HEAD content. Type changes (T) keep the same path
	on both sides, since Git still exposes HEAD content for the old type.
	Renames (R/C) retain both old and new paths so either side can be
	located.
	"""
	if not has_head:
		return [
			_ChangedPath(current_path=os.fsdecode(path), head_path=None)
			for path in _split_git_paths(
				_run_git(root, ["ls-files", "--cached", "-z"]).stdout
			)
		]

	output = _run_git(
		root,
		["diff", "--name-status", "--find-renames", "-z", "HEAD", "--"],
	).stdout
	parts = output.split(b"\0")
	changed_paths: list[_ChangedPath] = []
	index = 0

	while index < len(parts) - 1:
		status = os.fsdecode(parts[index])
		index += 1
		if status == "":
			continue

		if status.startswith(("R", "C")):
			old_path = os.fsdecode(parts[index])
			new_path = os.fsdecode(parts[index + 1])
			index += 2
			changed_paths.append(
				_ChangedPath(
					current_path=new_path,
					head_path=old_path,
					old_path=old_path,
					new_path=new_path,
				)
			)
			continue

		path = os.fsdecode(parts[index])
		index += 1
		if status.startswith("D"):
			changed_paths.append(_ChangedPath(current_path=None, head_path=path))
		elif status.startswith("A"):
			changed_paths.append(_ChangedPath(current_path=path, head_path=None))
		elif status.startswith("T"):
			changed_paths.append(_ChangedPath(current_path=path, head_path=path))
		else:
			changed_paths.append(_ChangedPath(current_path=path, head_path=path))

	return changed_paths


def _untracked_paths(root: Path) -> list[str]:
	"""List untracked paths that Git is not ignoring."""
	output = _run_git(
		root,
		["ls-files", "--others", "--exclude-standard", "-z"],
	).stdout
	return [os.fsdecode(path) for path in _split_git_paths(output)]


def _split_git_paths(output: bytes) -> list[bytes]:
	"""Split NUL-delimited Git output into non-empty path segments."""
	return [path for path in output.split(b"\0") if path]


def _changed_path_for_untracked(path: str) -> _ChangedPath:
	"""Build a _ChangedPath for an untracked file, which has no HEAD content."""
	return _ChangedPath(current_path=path, head_path=None)


def _file_contents(root: Path, changed_path: _ChangedPath) -> _FileContents:
	"""Read the decoded current and HEAD text for a changed path."""
	current = _read_current_text(root, changed_path.current_path)
	head = _read_head_text(root, changed_path.head_path)
	return _FileContents(changed_path, current, head)


def _read_current_text(root: Path, path: str | None) -> str | None:
	"""Read a working-tree file's decoded text, or None if it is missing, a symlink, or unreadable."""
	if path is None:
		return None

	file_path = root / path
	if file_path.is_symlink():
		return None

	try:
		return _decode_text(file_path.read_bytes())
	except OSError:
		return None


def _read_head_text(root: Path, path: str | None) -> str | None:
	"""Read a path's decoded HEAD text, or None if it has no HEAD version."""
	if path is None:
		return None

	try:
		output = _run_git(root, ["show", f"HEAD:{path}"]).stdout
	except GitCommandError:
		return None

	return _decode_text(output)


def _decode_text(data: bytes) -> str | None:
	"""Decode UTF-8 text, or return None for binary or undecodable content."""
	if b"\0" in data:
		return None

	try:
		return data.decode("utf-8")
	except UnicodeDecodeError:
		return None


def _find_matches(selection: str, content: str | None) -> list[int]:
	"""Return every start offset where `selection` occurs in `content`."""
	if content is None:
		return []

	positions: list[int] = []
	search_from = 0

	while True:
		position = content.find(selection, search_from)
		if position == -1:
			return positions

		positions.append(position)
		search_from = position + 1


def _make_locations(
	selection: str,
	positions: list[int],
	changed_path: _ChangedPath,
	content: str | None,
	side: str,
) -> list[SelectionLocation]:
	"""Build a SelectionLocation for each match position on the given side."""
	if content is None:
		return []

	return [
		SelectionLocation(
			path=(
				changed_path.current_path
				if side == "current"
				else changed_path.head_path
			),
			side=side,
			**_coordinates(content, position, len(selection)),
			old_path=changed_path.old_path,
			new_path=changed_path.new_path,
		)
		for position in positions
	]


def _coordinates(content: str, start: int, length: int) -> dict[str, int]:
	"""Return the one-based, end-exclusive line and column range for a match."""
	end = start + length
	start_line, start_column = _line_column(content, start)
	end_line, end_column = _line_column(content, end)
	return {
		"start_line": start_line,
		"start_column": start_column,
		"end_line": end_line,
		"end_column": end_column,
	}


def _line_column(content: str, offset: int) -> tuple[int, int]:
	"""Return the one-based line and column at a character offset."""
	line = content.count("\n", 0, offset) + 1
	last_newline = content.rfind("\n", 0, offset)
	column = offset - last_newline
	return line, column


def _selection_spans_sides(selection: str, file_contents: list[_FileContents]) -> bool:
	"""Return whether `selection` combines removed-only lines with current-only lines from the same file.

	This catches a copy that straddles a diff hunk boundary (part removed,
	part added) so it can be rejected instead of silently matched to one
	side.
	"""
	for contents in file_contents:
		if contents.current is None or contents.head is None:
			continue

		old_only, new_only = _changed_lines(contents.head, contents.current)
		if _has_line_fragment(selection, old_only) and _has_line_fragment(
			selection, new_only
		):
			return True

	return False


def _changed_lines(head: str, current: str) -> tuple[list[str], list[str]]:
	"""Return the lines HEAD and current don't share, using a line-level diff."""
	head_lines = head.splitlines(keepends=True)
	current_lines = current.splitlines(keepends=True)
	old_only: list[str] = []
	new_only: list[str] = []

	for (
		tag,
		head_start,
		head_end,
		current_start,
		current_end,
	) in difflib.SequenceMatcher(
		a=head_lines,
		b=current_lines,
		autojunk=False,
	).get_opcodes():
		if tag in ("delete", "replace"):
			old_only.extend(head_lines[head_start:head_end])
		if tag in ("insert", "replace"):
			new_only.extend(current_lines[current_start:current_end])

	return old_only, new_only


def _has_line_fragment(selection: str, lines: list[str]) -> bool:
	"""Return whether any non-blank line of `selection` appears within `lines`."""
	for fragment in selection.splitlines(keepends=True):
		fragment = fragment.rstrip("\r\n")
		if not fragment.strip():
			continue

		if any(fragment in line.rstrip("\r\n") for line in lines):
			return True

	return False
