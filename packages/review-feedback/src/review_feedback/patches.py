"""Create, check and refresh Git patches that split uncommitted work into proposed commits."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


# The number of unchanged lines shown around each change in a patch. It is
# fixed so every patch in a set, and every refresh of one, shows the same context.
CONTEXT_LINES = 10

# Where patches, metadata and the manifest go when --output-dir is not given,
# relative to the repository root. The repository should ignore this folder.
DEFAULT_OUTPUT_DIRECTORY = Path(".agent/review-patches")

# The version of the manifest and metadata layout. Raise it when a change would
# stop older snapshots from being read or checked.
FORMAT_VERSION = 1

# Matches the header line that starts each hunk in a unified diff.
HUNK_HEADER = re.compile(r"^@@ ")


class PatchError(RuntimeError):
	"""Report an invalid plan, repository state, or generated patch."""


def git_command(
	root: Path,
	arguments: list[str],
	*,
	input_bytes: bytes | None = None,
	env: dict[str, str] | None = None,
	expected_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
	"""Run a Git command in `root`, raising PatchError unless it exits with one of `expected_codes`.

	Output stays as raw bytes so binary patches and non-UTF-8 paths are not altered.
	"""
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		env=env,
		input=input_bytes,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		check=False,
	)

	if result.returncode not in expected_codes:
		detail = result.stderr.decode("utf-8", errors="replace").strip()

		raise PatchError(
			f"git {' '.join(arguments)} failed with exit {result.returncode}: {detail}"
		)

	return result


def git_text(
	root: Path, arguments: list[str], *, expected_codes: tuple[int, ...] = (0,)
) -> str:
	"""Run a Git command in `root` and return its output as text, keeping undecodable path bytes intact."""
	return git_command(root, arguments, expected_codes=expected_codes).stdout.decode(
		"utf-8", errors="surrogateescape"
	)


def nul_values(value: bytes) -> list[str]:
	"""Split NUL-separated Git output into a sorted list of paths."""
	return sorted(
		item.decode("utf-8", errors="surrogateescape")
		for item in value.split(b"\0")
		if item
	)


def repository_root(path: Path) -> Path:
	"""Return the absolute root of the Git repository containing `path`."""
	root = git_text(path, ["rev-parse", "--show-toplevel"]).strip()

	return Path(root).resolve()


def ensure_head(root: Path) -> str:
	"""Return the commit hash of `HEAD`, which every generated patch is based on."""
	return git_text(root, ["rev-parse", "--verify", "HEAD"]).strip()


def has_staged_changes(root: Path) -> bool:
	"""Return whether the real index contains staged content."""
	result = git_command(root, ["diff", "--cached", "--quiet"], expected_codes=(0, 1))

	return result.returncode == 1


def changed_paths(root: Path) -> list[str]:
	"""Return every changed tracked path and every untracked, non-ignored path, sorted and relative to the root."""
	tracked = git_command(
		root,
		[
			"diff",
			"--name-only",
			"--no-renames",
			"-z",
			"HEAD",
			"--",
		],
	).stdout
	untracked = git_command(
		root,
		["ls-files", "--others", "--exclude-standard", "-z"],
	).stdout

	return sorted(set(nul_values(tracked) + nul_values(untracked)))


def path_is_tracked_at_head(root: Path, path: str) -> bool:
	"""Return whether `path` exists in the committed base tree."""
	result = git_command(
		root,
		["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", path],
	)

	return path in nul_values(result.stdout)


def validate_relative_path(path: str) -> str:
	"""Return `path` unchanged, raising PatchError if it is empty, absolute, uses `..`, or contains a line break or NUL."""
	if not path or "\n" in path or "\r" in path or "\0" in path:
		raise PatchError(f"invalid plan path: {path!r}")

	if Path(path).is_absolute() or ".." in Path(path).parts:
		raise PatchError(f"plan path must stay inside the repository: {path!r}")

	return path


def raw_diff(root: Path, path: str) -> str:
	"""Return the diff of `path` against `HEAD`, showing an untracked path as a new file.

	The diff includes binary content and full blob hashes so the patch applies exactly.
	"""
	common = [
		"diff",
		"--binary",
		"--full-index",
		"--no-ext-diff",
		"--no-renames",
		f"--unified={CONTEXT_LINES}",
	]

	if path_is_tracked_at_head(root, path):
		result = git_command(root, [*common, "HEAD", "--", path])
	else:
		result = git_command(
			root,
			[*common, "--no-index", "/dev/null", path],
			expected_codes=(0, 1),
		)

	return result.stdout.decode("utf-8", errors="surrogateescape")


def hunk_ranges(block: str) -> list[tuple[int, int]]:
	"""Return the start and end line of each hunk in one file's diff."""
	lines = block.splitlines(keepends=True)
	starts = [index for index, line in enumerate(lines) if HUNK_HEADER.match(line)]

	return [
		(start, starts[position + 1] if position + 1 < len(starts) else len(lines))
		for position, start in enumerate(starts)
	]


def select_hunks(
	block: str, selected: list[int] | None, path: str
) -> tuple[str, list[int]]:
	"""Return the file's diff with only the `selected` hunks, plus the sorted hunk numbers used.

	The file header is always kept. When `selected` is None, the whole diff and every hunk
	number are returned. A file with no text hunks, such as a binary change, can only be
	selected whole.
	"""
	ranges = hunk_ranges(block)

	if selected is None:
		return block, list(range(len(ranges)))

	if not ranges:
		raise PatchError(f"path {path!r} has no textual hunks and must be whole-file")

	if len(set(selected)) != len(selected) or any(
		index < 0 or index >= len(ranges) for index in selected
	):
		raise PatchError(f"invalid hunk selection for {path!r}: {selected!r}")

	lines = block.splitlines(keepends=True)
	prefix_end = ranges[0][0]
	selected_lines = lines[:prefix_end]
	for index in sorted(selected):
		start, end = ranges[index]
		selected_lines.extend(lines[start:end])

	return "".join(selected_lines), sorted(selected)


def content_bytes(root: Path, path: str) -> bytes | None:
	"""Return the worktree bytes of `path`, the target of a symbolic link, or None when the file is deleted."""
	file_path = root / Path(path)

	if not file_path.exists() and not file_path.is_symlink():
		return None

	if file_path.is_symlink():
		return os.readlink(file_path).encode("utf-8", errors="surrogateescape")

	return file_path.read_bytes()


def head_bytes(root: Path, path: str) -> bytes | None:
	"""Return the bytes of `path` at `HEAD`, or None when the file is new."""
	if not path_is_tracked_at_head(root, path):
		return None

	return git_command(root, ["show", f"HEAD:{path}"]).stdout


def sha256(value: bytes | None) -> str | None:
	"""Return the SHA-256 hex digest of `value`, or None for a file that does not exist."""
	if value is None:
		return None

	return hashlib.sha256(value).hexdigest()


def load_plan(plan_path: Path) -> list[dict[str, Any]]:
	"""Read the plan file and return its proposals, each with an `id`, `title` and `changes`.

	A proposal without a title uses its ID. Raises PatchError when the file cannot be read,
	has no proposals, or a proposal has an invalid or repeated ID or no changes.
	"""
	try:
		plan = json.loads(plan_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as error:
		raise PatchError(f"cannot read plan {plan_path}: {error}") from error

	if (
		not isinstance(plan, dict)
		or not isinstance(plan.get("proposals"), list)
		or not plan["proposals"]
	):
		raise PatchError("plan must contain a non-empty proposals list")

	proposals: list[dict[str, Any]] = []
	proposal_ids: set[str] = set()

	for proposal in plan["proposals"]:
		if not isinstance(proposal, dict):
			raise PatchError("each proposal must be an object")

		proposal_id = proposal.get("id")
		changes = proposal.get("changes")

		if not isinstance(proposal_id, str) or not re.fullmatch(
			r"[A-Za-z0-9][A-Za-z0-9._-]*", proposal_id
		):
			raise PatchError(f"invalid proposal id: {proposal_id!r}")

		if proposal_id in proposal_ids:
			raise PatchError(f"duplicate proposal id: {proposal_id}")

		if not isinstance(changes, list) or not changes:
			raise PatchError(f"proposal {proposal_id} must contain changes")

		proposal_ids.add(proposal_id)
		proposals.append(
			{
				"id": proposal_id,
				"title": proposal.get("title", proposal_id),
				"changes": changes,
			}
		)

	return proposals


def proposal_units(
	proposal: dict[str, Any],
	diffs: dict[str, str],
) -> tuple[set[tuple[str, int | str]], dict[str, list[int] | None]]:
	"""Check one proposal's changes and return the hunks it claims and its selection for each path.

	A claimed hunk is a (path, hunk number) pair, or (path, "whole") for a file with no
	text hunks. A path taken whole maps to None in the selection. Raises PatchError for an
	invalid change or hunk list, or an unchanged or repeated path.
	"""
	units: set[tuple[str, int | str]] = set()
	selections: dict[str, list[int] | None] = {}
	seen_paths: set[str] = set()

	for change in proposal["changes"]:
		if not isinstance(change, dict) or not isinstance(change.get("path"), str):
			raise PatchError(f"proposal {proposal['id']} contains an invalid change")

		path = validate_relative_path(change["path"])

		if path in seen_paths:
			raise PatchError(f"proposal {proposal['id']} repeats path {path!r}")

		if path not in diffs:
			raise PatchError(
				f"proposal {proposal['id']} does not match changed path {path!r}"
			)

		seen_paths.add(path)
		selected = change.get("hunks")

		if selected is not None and (
			not isinstance(selected, list)
			or not all(isinstance(index, int) for index in selected)
		):
			raise PatchError(f"hunks for {path!r} must be a list of integers")

		block, hunk_indexes = select_hunks(diffs[path], selected, path)

		if selected is None:
			units.update((path, index) for index in hunk_indexes)

			if not hunk_indexes:
				units.add((path, "whole"))
		else:
			units.update((path, index) for index in hunk_indexes)

		selections[path] = None if selected is None else hunk_indexes

		if not block:
			raise PatchError(
				f"proposal {proposal['id']} selected no patch content for {path!r}"
			)

	return units, selections


def apply_check(root: Path, patch: bytes) -> None:
	"""Raise PatchError unless `patch` applies cleanly to `HEAD`.

	The check uses a temporary index, so the real index and worktree are left alone.
	"""
	with tempfile.TemporaryDirectory(prefix="review-patches-index-") as directory:
		index_path = Path(directory) / "index"
		env = os.environ.copy()
		env["GIT_INDEX_FILE"] = str(index_path)

		git_command(root, ["read-tree", "HEAD"], env=env)
		git_command(
			root,
			["apply", "--check", "--cached", "--whitespace=nowarn", "-"],
			input_bytes=patch,
			env=env,
		)


def file_record(root: Path, path: str) -> dict[str, Any]:
	"""Return the SHA-256 of `path` at `HEAD` and in the worktree, so a later check can spot changes."""
	current = content_bytes(root, path)
	base = head_bytes(root, path)

	return {
		"path": path,
		"base_sha256": sha256(base),
		"worktree_sha256": sha256(current),
	}


def write_json(path: Path, value: dict[str, Any]) -> None:
	"""Write `value` as tab-indented JSON with sorted keys, so the same data always gives the same file."""
	path.write_text(
		json.dumps(value, ensure_ascii=False, indent="\t", sort_keys=True) + "\n",
		encoding="utf-8",
	)


def generate(
	root: Path,
	plan_path: Path,
	output_directory: Path,
	*,
	staged_policy: str,
	refresh_id: str | None = None,
) -> dict[str, Any]:
	"""Write a patch and metadata file for each proposal in the plan, then write and return the manifest.

	Every changed hunk must belong to exactly one proposal. Plan errors, unclaimed or
	double-claimed hunks, and staged changes (unless `staged_policy` is "include") raise
	PatchError before anything is written; a patch that does not apply to `HEAD` also
	raises it. With `refresh_id`, only that proposal's files are rewritten and the others
	keep their existing files and manifest entries. Tracked files and the index are
	never changed.
	"""
	base_revision = ensure_head(root)

	if staged_policy not in {"refuse", "include"}:
		raise PatchError(f"unsupported staged policy: {staged_policy}")

	if has_staged_changes(root) and staged_policy == "refuse":
		raise PatchError(
			"the index contains staged changes; choose an explicit staged policy"
		)

	proposals = load_plan(plan_path)
	current_paths = changed_paths(root)

	if not current_paths:
		raise PatchError("the worktree has no tracked or untracked changes")

	diffs = {path: raw_diff(root, path) for path in current_paths}
	all_units: set[tuple[str, int | str]] = set()
	proposal_records: list[dict[str, Any]] = []

	for proposal in proposals:
		units, selections = proposal_units(proposal, diffs)
		duplicate_units = all_units.intersection(units)

		if duplicate_units:
			raise PatchError(
				f"units assigned to more than one proposal: {sorted(duplicate_units)!r}"
			)

		all_units.update(units)
		proposal_records.append({"proposal": proposal, "selections": selections})

	if not all_units:
		raise PatchError("plan selected no changed units")

	expected_units: set[tuple[str, int | str]] = set()

	for path, diff in diffs.items():
		ranges = hunk_ranges(diff)
		if ranges:
			expected_units.update((path, index) for index in range(len(ranges)))
		else:
			expected_units.add((path, "whole"))

	missing_units = expected_units - all_units

	if missing_units:
		raise PatchError(
			f"changed units are missing from the plan: {sorted(missing_units)!r}"
		)

	if refresh_id is not None and refresh_id not in {
		record["proposal"]["id"] for record in proposal_records
	}:
		raise PatchError(f"cannot refresh unknown proposal {refresh_id!r}")

	plan_hash = sha256(plan_path.read_bytes())
	existing_manifest_proposals: dict[str, dict[str, Any]] = {}

	if refresh_id is not None:
		manifest_path = output_directory / "manifest.json"

		try:
			existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError) as error:
			raise PatchError(
				f"cannot refresh without an existing manifest: {error}"
			) from error

		if not isinstance(existing_manifest, dict) or not isinstance(
			existing_manifest.get("proposals"), list
		):
			raise PatchError("cannot refresh from a manifest without a proposals list")

		existing_manifest_proposals = {
			proposal["id"]: proposal
			for proposal in existing_manifest["proposals"]
			if isinstance(proposal, dict) and isinstance(proposal.get("id"), str)
		}
		missing_manifest_proposals = {
			record["proposal"]["id"]
			for record in proposal_records
			if record["proposal"]["id"] not in existing_manifest_proposals
		}

		if missing_manifest_proposals:
			raise PatchError(
				"cannot refresh because the manifest is missing proposals: "
				f"{sorted(missing_manifest_proposals)!r}"
			)

	output_directory.mkdir(parents=True, exist_ok=True)
	manifest_proposals: list[dict[str, Any]] = []

	for record in proposal_records:
		proposal = record["proposal"]
		selections = record["selections"]
		patch_parts: list[str] = []
		change_records: list[dict[str, Any]] = []

		for path in sorted(selections):
			selected = selections[path]
			selected_patch, selected_hunks = select_hunks(diffs[path], selected, path)
			patch_parts.append(selected_patch)
			change_records.append({**file_record(root, path), "hunks": selected_hunks})

		patch_bytes = "".join(patch_parts).encode("utf-8", errors="surrogateescape")

		if not patch_bytes:
			raise PatchError(f"proposal {proposal['id']} produced an empty patch")

		apply_check(root, patch_bytes)

		patch_path = output_directory / f"{proposal['id']}.patch"
		metadata_path = output_directory / f"{proposal['id']}.json"
		metadata = {
			"format_version": FORMAT_VERSION,
			"proposal": {"id": proposal["id"], "title": proposal["title"]},
			"base": base_revision,
			"context_lines": CONTEXT_LINES,
			"staged_policy": staged_policy,
			"diff_options": [
				"--binary",
				"--full-index",
				"--no-ext-diff",
				"--no-renames",
			],
			"changes": change_records,
			"patch": {"path": patch_path.name, "sha256": sha256(patch_bytes)},
			"freshness": "fresh",
			"apply_check": "passed",
		}
		manifest_proposal = {
			"id": proposal["id"],
			"patch": patch_path.name,
			"metadata": metadata_path.name,
			"sha256": metadata["patch"]["sha256"],
		}

		if refresh_id is None or proposal["id"] == refresh_id:
			patch_path.write_bytes(patch_bytes)
			write_json(metadata_path, metadata)
			manifest_proposals.append(manifest_proposal)
		else:
			manifest_proposals.append(existing_manifest_proposals[proposal["id"]])

	manifest = {
		"format_version": FORMAT_VERSION,
		"base": base_revision,
		"context_lines": CONTEXT_LINES,
		"staged_policy": staged_policy,
		"plan_sha256": plan_hash,
		"proposals": manifest_proposals,
	}

	write_json(output_directory / "manifest.json", manifest)

	return manifest


def check_metadata(
	root: Path, metadata_path: Path, base_revision: str
) -> tuple[bool, list[str]]:
	"""Compare one proposal's metadata with `HEAD`, its patch file and the current file contents.

	Returns whether the proposal is still fresh, and a list of what changed when it is not.
	"""
	try:
		metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as error:
		return False, [f"cannot read metadata: {error}"]

	issues: list[str] = []

	if metadata.get("base") != base_revision:
		issues.append("base revision changed")

	patch_name = metadata.get("patch", {}).get("path")

	if not isinstance(patch_name, str):
		return False, ["metadata has no patch path"]

	patch_path = metadata_path.parent / patch_name

	try:
		patch_bytes = patch_path.read_bytes()
	except OSError as error:
		return False, [f"cannot read patch: {error}"]

	if sha256(patch_bytes) != metadata.get("patch", {}).get("sha256"):
		issues.append("patch hash changed")

	for change in metadata.get("changes", []):
		path = change.get("path")

		if not isinstance(path, str):
			issues.append("metadata contains an invalid path")
			continue

		current = file_record(root, path)

		for key in ("base_sha256", "worktree_sha256"):
			if current[key] != change.get(key):
				issues.append(f"{path}: {key} changed")

	return not issues, issues


def check_directory(root: Path, directory: Path, plan_path: Path) -> int:
	"""Print whether each proposal in `directory` is fresh or stale; return 0 when all are fresh, else 1.

	A changed plan or a new `HEAD` commit makes the whole set stale. An unreadable manifest
	or plan is reported as stale rather than raised.
	"""
	manifest_path = directory / "manifest.json"

	try:
		manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as error:
		print(f"stale: cannot read manifest: {error}")
		return 1

	base_revision = ensure_head(root)

	try:
		current_plan_hash = sha256(plan_path.read_bytes())
	except OSError as error:
		print(f"stale: cannot read plan: {error}")
		return 1

	stale = False

	if manifest.get("plan_sha256") != current_plan_hash:
		print("stale: plan changed")
		stale = True

	if manifest.get("base") != base_revision:
		print("stale: base revision changed")
		stale = True

	for proposal in manifest.get("proposals", []):
		metadata_name = proposal.get("metadata")

		if not isinstance(metadata_name, str):
			print(f"stale: proposal {proposal.get('id', '<unknown>')} has no metadata")
			stale = True
			continue

		fresh, issues = check_metadata(root, directory / metadata_name, base_revision)

		if fresh:
			print(f"fresh: {proposal['id']}")
		else:
			stale = True
			print(f"stale: {proposal.get('id', '<unknown>')}: {'; '.join(issues)}")

	return int(stale)


def parse_arguments(arguments: list[str] | None = None) -> argparse.Namespace:
	"""Read the command line for the create, check and refresh subcommands."""
	parser = argparse.ArgumentParser(description=__doc__)
	commands = parser.add_subparsers(dest="command", required=True)

	create = commands.add_parser("create", help="Create patches from a plan.")
	create.add_argument("--plan", type=Path, required=True)
	create.add_argument("--root", type=Path, default=Path.cwd())
	create.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
	create.add_argument(
		"--staged-policy", choices=("refuse", "include"), default="refuse"
	)

	check = commands.add_parser("check", help="Check generated patches for freshness.")
	check.add_argument("directory", type=Path)
	check.add_argument("--plan", type=Path)
	check.add_argument("--root", type=Path, default=Path.cwd())

	refresh = commands.add_parser("refresh", help="Refresh one proposal patch.")
	refresh.add_argument("proposal_id")
	refresh.add_argument("--plan", type=Path, required=True)
	refresh.add_argument("--root", type=Path, default=Path.cwd())
	refresh.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
	refresh.add_argument(
		"--staged-policy", choices=("refuse", "include"), default="refuse"
	)

	return parser.parse_args(arguments)


def run(arguments: argparse.Namespace) -> int:
	"""Run the chosen subcommand and return its exit status.

	Relative plan, output and check paths are resolved from the repository root, not the
	current directory. Create and refresh print the manifest as JSON.
	"""
	root = repository_root(arguments.root.resolve())

	if arguments.command == "check":
		directory = (
			arguments.directory
			if arguments.directory.is_absolute()
			else root / arguments.directory
		)
		plan_path = (
			arguments.plan if arguments.plan is not None else directory / "plan.json"
		)

		if not plan_path.is_absolute():
			plan_path = root / plan_path

		return check_directory(root, directory, plan_path)

	plan_path = (
		arguments.plan if arguments.plan.is_absolute() else root / arguments.plan
	)

	output_directory = (
		arguments.output_dir
		if arguments.output_dir.is_absolute()
		else root / arguments.output_dir
	)
	manifest = generate(
		root,
		plan_path,
		output_directory,
		staged_policy=arguments.staged_policy,
		refresh_id=arguments.proposal_id if arguments.command == "refresh" else None,
	)

	print(json.dumps(manifest, ensure_ascii=False, indent="\t", sort_keys=True))

	return 0


def main() -> int:
	"""Run `review-patches`, printing plan and repository errors to standard error with exit status 2."""
	try:
		return run(parse_arguments())
	except PatchError as error:
		print(f"error: {error}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
