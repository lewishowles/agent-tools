"""Draft an outside review brief from a progress chunk."""

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from review_packet.brief import render
from review_packet.manifest import create as create_manifest
from review_packet.manifest import to_json as manifest_to_json
from review_packet.progress_records import (
	Chunk,
	ProgressError,
	RelatedTasks,
	read_chunk,
	read_related_tasks,
)
from review_packet.selection import (
	MissingIncludeError,
	OutsideSymlinkError,
	select_files,
)
from review_packet.settings import (
	SETTINGS_NAME,
	InvalidSettingsError,
	Settings,
	read_settings,
)
from review_packet.snapshot import (
	GitError,
	InvalidBaseError,
	Snapshot,
	capture,
	repository_name,
	repository_root,
)


class BriefExistsError(ValueError):
	"""The draft brief already exists and force was not requested."""


# Failures the user can fix by changing the command or the settings file.
USAGE_ERRORS = (
	BriefExistsError,
	InvalidBaseError,
	InvalidSettingsError,
	MissingIncludeError,
	OutsideSymlinkError,
)

# Failures in Git or the file system that stop the draft. Progress failures
# are handled separately so they keep their own exit code.
ENVIRONMENT_ERRORS = (GitError, OSError)

# The exit code for a usage failure, matching argparse.
USAGE_EXIT = 2

# The exit code for an environment failure.
ENVIRONMENT_EXIT = 3

# The exit code when progress cannot find a requested record.
NOT_FOUND_EXIT = 1

# The exit code for each progress error kind, from docs/cli-contract.md.
PROGRESS_EXITS = {
	"usage": USAGE_EXIT,
	"invalid-id": USAGE_EXIT,
	"wrong-id-type": USAGE_EXIT,
	"not-found": NOT_FOUND_EXIT,
	"environment": ENVIRONMENT_EXIT,
	"internal": ENVIRONMENT_EXIT,
}


@dataclass(frozen=True)
class DraftResult:
	"""The paths printed after a draft is written."""

	# The absolute path lets the next command find the brief.
	brief: str
	# The absolute path lets the next command find the manifest.
	manifest: str
	# The selected repository paths preview the packet contents.
	contents: tuple[str, ...]
	# The relative settings path avoids leaking the user's home directory.
	settings: str | None


class Parser(argparse.ArgumentParser):
	"""An argument parser that reports errors as JSON when asked to."""

	# Whether --json was passed. It lives on the class because argparse builds the
	# draft subparser as another Parser, and that subparser reports most argument errors.
	json_mode = False

	def error(self, message: str) -> None:
		"""Report invalid arguments and exit.

		Args:
			message: The argparse error message.

		Raises:
			SystemExit: Always, with the usage exit code.
		"""
		if self.json_mode:
			_print_error("usage", message, json_mode=True)
			raise SystemExit(USAGE_EXIT)

		super().error(message)


def build_parser() -> argparse.ArgumentParser:
	"""Describe the draft command and its options.

	Returns:
		The parser for the review-packet command.
	"""
	# The top-level parser; its error method can print JSON.
	parser = Parser(prog="review-packet")
	# The subcommand group; draft is the only subcommand so far.
	commands = parser.add_subparsers(dest="command", required=True)
	# The draft subcommand and its options.
	draft = commands.add_parser(
		"draft", description="Write a review brief and preview its selected files."
	)

	draft.add_argument("chunk_id", help="The progress chunk to review.")
	draft.add_argument("--base", help="Git revision to compare with instead of HEAD.")
	draft.add_argument(
		"--include",
		action="append",
		default=[],
		help="Add a repository file; repeat for more files.",
	)
	draft.add_argument(
		"--force", action="store_true", help="Replace an existing draft brief."
	)
	draft.add_argument(
		"--json", action="store_true", help="Print a JSON result or error."
	)

	return parser


def main(argv: list[str] | None = None) -> int:
	"""Draft the brief and print its path and selected contents.

	Args:
		argv: The command-line arguments, or None to read sys.argv.

	Returns:
		0 when the brief is written, 1 when a record is missing, 2 for a
		usage failure and 3 for an environment or internal failure.
	"""
	# The arguments to parse, without the program name.
	arguments = argv if argv is not None else sys.argv[1:]
	# The parser for the draft command.
	parser = build_parser()

	Parser.json_mode = "--json" in arguments
	# The parsed command-line options.
	args = parser.parse_args(arguments)

	try:
		# The worktree the command runs in.
		root = repository_root(Path.cwd())
		# The chunk under review, with its task.
		chunk = read_chunk(root, args.chunk_id)
		# Titles of tasks linked to the chunk's task.
		related = read_related_tasks(root, chunk.task.id)
		# The repository's optional settings.
		settings = read_settings(root)
		# The current changes since the base.
		snapshot = capture(root, args.base)
		# The repository files for the packet.
		contents = select_files(root, snapshot, tuple(args.include) + settings.include)
		# Where the brief is written.
		brief_path = _brief_path(root, args.chunk_id)
		# The paths to report.
		data = _write_draft(
			root, brief_path, chunk, snapshot, contents, settings, related, args.force
		)
	except USAGE_ERRORS as error:
		_print_error("usage", str(error), args.json)
		return USAGE_EXIT
	except ProgressError as error:
		# Progress's error kind, or environment for a kind the contract lacks.
		code = error.code if error.code in PROGRESS_EXITS else "environment"
		_print_error(code, str(error), args.json)
		return PROGRESS_EXITS[code]
	except ENVIRONMENT_ERRORS as error:
		_print_error("environment", str(error), args.json)
		return ENVIRONMENT_EXIT

	_print_result(data, args.json)

	return 0


def _brief_path(root: Path, chunk_id: str) -> Path:
	"""Choose the draft brief path in the system temporary folder.

	Args:
		root: The repository worktree root.
		chunk_id: The chunk being reviewed.

	Returns:
		The destination brief path.

	Raises:
		GitError: Git cannot locate the shared repository name.
	"""
	# One folder per repository and chunk, so drafts do not overwrite each other.
	folder = (
		Path(tempfile.gettempdir())
		/ "review-packet"
		/ f"{repository_name(root)}-{chunk_id}"
	)

	return folder / "REVIEW.md"


def _write_draft(
	root: Path,
	brief_path: Path,
	chunk: Chunk,
	snapshot: Snapshot,
	contents: tuple[str, ...],
	settings: Settings,
	related: RelatedTasks,
	force: bool,
) -> DraftResult:
	"""Write the brief and manifest, then return their paths.

	Args:
		root: The repository root containing the selected files.
		brief_path: The destination file.
		chunk: The reviewed chunk and its task.
		snapshot: The current Git change set.
		contents: The selected repository files.
		settings: The optional settings file and its values.
		related: Titles of prerequisite and dependent tasks.
		force: Whether an existing brief may be replaced.

	Returns:
		The paths to report to the user.

	Raises:
		BriefExistsError: A brief exists and force is false.
		OSError: A selected file cannot be read or a draft file cannot be written.
	"""
	# The settings file name to show, or None when there is no settings file.
	settings_name = SETTINGS_NAME if settings.found else None

	if brief_path.exists() and not force:
		raise BriefExistsError(
			f"The review brief already exists: {brief_path}. Use --force to replace it."
		)

	# Hash every selected path before either draft file is replaced.
	manifest = create_manifest(root, snapshot, contents)
	# The manifest sits beside REVIEW.md, where the build step looks for it.
	manifest_path = brief_path.with_name("manifest.json")

	brief_path.parent.mkdir(parents=True, exist_ok=True)
	# The brief is written first. If the manifest write then fails, the old
	# manifest either still matches the files or shows that they changed, whereas
	# a new manifest beside an old brief would pass off older work as current.
	brief_path.write_text(render(chunk, snapshot, contents, settings_name, related))
	manifest_path.write_text(json.dumps(manifest_to_json(manifest), indent=2) + "\n")

	return DraftResult(
		str(brief_path.resolve()),
		str(manifest_path.resolve()),
		contents,
		settings_name,
	)


def _print_result(data: DraftResult, json_mode: bool) -> None:
	"""Print the draft result in JSON or readable text.

	Args:
		data: The drafted brief and selected paths.
		json_mode: Whether to print a JSON envelope.
	"""
	if json_mode:
		# The success envelope shared with other agent-tools commands.
		envelope = {
			"ok": True,
			"data": {
				"brief": data.brief,
				"manifest": data.manifest,
				"contents": list(data.contents),
				"settings": data.settings,
			},
		}

		print(json.dumps(envelope))
		return

	print(f"Brief: {data.brief}")
	print(f"Manifest: {data.manifest}")
	print(f"Settings: {data.settings or '(none)'}")
	print("Contents:")

	for path in data.contents:
		print(f"- {path}")


def _print_error(code: str, message: str, json_mode: bool) -> None:
	"""Print a failure in JSON on stdout or as text on stderr.

	Args:
		code: The shared CLI error kind.
		message: The message for the user.
		json_mode: Whether to print a JSON envelope.
	"""
	if json_mode:
		print(json.dumps({"ok": False, "error": {"code": code, "message": message}}))
		return

	print(f"Error: {message}", file=sys.stderr)


if __name__ == "__main__":
	raise SystemExit(main())
