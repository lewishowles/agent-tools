"""Command-line interface for progress read commands."""

import argparse
import json
import sys

from .database import Database
from .errors import ProgressError
from .projects import ProjectStore
from .reads import DEFAULT_LIMIT, MAX_LIMIT, ReadStore
from .render import render


class CliUsageError(Exception):
	"""Signal a parser error to format through the stable JSON envelope."""


class ProgressArgumentParser(argparse.ArgumentParser):
	"""Raise CliUsageError on a bad argument instead of exiting the process directly."""

	def error(self, message: str) -> None:
		"""Turn an argparse usage error into a CliUsageError the caller can format."""
		raise CliUsageError(message)


def _add_output_options(parser: argparse.ArgumentParser) -> None:
	"""Add the shared --json and --database options to a command parser."""
	parser.add_argument(
		"--json",
		action="store_true",
		default=argparse.SUPPRESS,
		help="write the stable agent response envelope",
	)
	parser.add_argument(
		"--database",
		metavar="PATH",
		default=argparse.SUPPRESS,
		help="use PATH instead of ~/.agents/progress.db",
	)


def _add_page_options(parser: argparse.ArgumentParser) -> None:
	"""Add the shared --limit and --offset options to a list command parser."""
	parser.add_argument(
		"--limit",
		type=_page_number,
		default=DEFAULT_LIMIT,
		help=f"maximum records to show, 1-{MAX_LIMIT} (default: {DEFAULT_LIMIT})",
	)
	parser.add_argument(
		"--offset",
		type=_offset_number,
		default=0,
		help="number of records to skip (default: 0)",
	)


def build_parser() -> argparse.ArgumentParser:
	"""Build the full progress command parser with every read subcommand."""
	parser = ProgressArgumentParser(
		prog="progress",
		description="Track project progress in a local SQLite database.",
	)
	parser.add_argument(
		"--json",
		action="store_true",
		default=False,
		help="write the stable agent response envelope",
	)
	parser.add_argument(
		"--database",
		metavar="PATH",
		default=None,
		help="use PATH instead of ~/.agents/progress.db",
	)
	commands = parser.add_subparsers(dest="command", required=True)

	for name in ("next", "current"):
		command = commands.add_parser(
			name, help="show the current task and active chunk"
		)
		_add_output_options(command)

	project = commands.add_parser("project", help="manage the current project binding")
	project_commands = project.add_subparsers(dest="project_command", required=True)
	project_init = project_commands.add_parser("init", help="create and bind a project")
	project_init.add_argument("--slug", required=True)
	project_init.add_argument("--name", required=True)
	_add_output_options(project_init)
	project_attach = project_commands.add_parser(
		"attach", help="bind an existing project"
	)
	project_attach.add_argument("project_id")
	_add_output_options(project_attach)
	project_current = project_commands.add_parser(
		"current", help="show the bound project"
	)
	_add_output_options(project_current)

	release = commands.add_parser("release", help="read release records")
	release_commands = release.add_subparsers(dest="release_command", required=True)
	release_list = release_commands.add_parser("list", help="list releases")
	_add_page_options(release_list)
	_add_output_options(release_list)

	task = commands.add_parser("task", help="read task records")
	task_commands = task.add_subparsers(dest="task_command", required=True)
	task_get = task_commands.add_parser("get", help="show one task")
	task_get.add_argument("task_id")
	_add_output_options(task_get)
	task_list = task_commands.add_parser("list", help="list tasks")
	task_list.add_argument("--status")
	_add_page_options(task_list)
	_add_output_options(task_list)

	chunk = commands.add_parser("chunk", help="read chunk records")
	chunk_commands = chunk.add_subparsers(dest="chunk_command", required=True)
	chunk_list = chunk_commands.add_parser("list", help="list chunks for a task")
	chunk_list.add_argument("--task", required=True, dest="task_id")
	_add_page_options(chunk_list)
	_add_output_options(chunk_list)

	ready = commands.add_parser("ready", help="list tasks ready to start")
	_add_page_options(ready)
	_add_output_options(ready)

	return parser


def main(argv: list[str] | None = None) -> int:
	"""Parse arguments, run the matching command, and return the process exit status."""
	arguments = list(sys.argv[1:] if argv is None else argv)
	json_mode = "--json" in arguments

	try:
		args = build_parser().parse_args(arguments)
		json_mode = bool(getattr(args, "json", json_mode))
		data, command = _run_command(args)
	except CliUsageError as error:
		return _write_error(
			"usage",
			str(error),
			{},
			json_mode,
			status=2,
		)
	except ProgressError as error:
		return _write_error(error.code, error.message, error.details, json_mode)

	if json_mode:
		_write_json({"ok": True, "data": data})
	else:
		sys.stdout.write(render(command, data))

	return 0


def _run_command(args: argparse.Namespace) -> tuple[object, str]:
	"""Dispatch parsed arguments to the matching read or project store call."""
	database = Database(getattr(args, "database", None))
	command_name = args.command
	subcommand_name = getattr(args, f"{command_name}_command", None)
	dispatch = {
		("next", None): lambda: (ReadStore(database).next(), "next"),
		("current", None): lambda: (ReadStore(database).current(), "current"),
		("project", None): lambda: (_run_project(args, database), "project"),
		("release", "list"): lambda: (
			ReadStore(database).release_list(args.limit, args.offset),
			"release list",
		),
		("task", "get"): lambda: (
			ReadStore(database).task_get(args.task_id),
			"task get",
		),
		("task", "list"): lambda: (
			ReadStore(database).task_list(args.status, args.limit, args.offset),
			"task list",
		),
		("chunk", "list"): lambda: (
			ReadStore(database).chunk_list(args.task_id, args.limit, args.offset),
			"chunk list",
		),
		("ready", None): lambda: (
			ReadStore(database).ready(args.limit, args.offset),
			"ready",
		),
	}
	handler = dispatch.get((command_name, subcommand_name))
	if handler is None:
		raise CliUsageError("unknown progress command")

	return handler()


def _run_project(args: argparse.Namespace, database: Database) -> dict[str, object]:
	"""Run the requested project binding subcommand."""
	store = ProjectStore(database)
	if args.project_command == "init":
		return store.init(args.slug, args.name).to_dict()
	if args.project_command == "attach":
		return store.attach(args.project_id).to_dict()
	if args.project_command == "current":
		return store.current().to_dict()

	raise CliUsageError("unknown project command")


def _page_number(value: str) -> int:
	"""Parse --limit, rejecting anything outside the public 1..MAX_LIMIT bound."""
	try:
		number = int(value)
	except ValueError as error:
		raise argparse.ArgumentTypeError("limit must be an integer") from error
	if not 1 <= number <= MAX_LIMIT:
		raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_LIMIT}")

	return number


def _offset_number(value: str) -> int:
	"""Parse --offset, rejecting a negative value."""
	try:
		number = int(value)
	except ValueError as error:
		raise argparse.ArgumentTypeError("offset must be an integer") from error
	if number < 0:
		raise argparse.ArgumentTypeError("offset must be zero or greater")

	return number


def _write_error(
	code: str,
	message: str,
	details: dict[str, object],
	json_mode: bool,
	status: int = 1,
) -> int:
	"""Write one error in the mode the caller asked for and return its exit status."""
	if json_mode:
		_write_json(
			{
				"ok": False,
				"error": {"code": code, "message": message, "details": details},
			}
		)
	else:
		sys.stderr.write(f"Error: {message}\n")

	return status


def _write_json(value: object) -> None:
	"""Write one compact JSON value to stdout with no surrounding prose."""
	sys.stdout.write(
		json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
	)


if __name__ == "__main__":
	raise SystemExit(main())
