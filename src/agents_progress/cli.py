"""Command-line interface for progress read and write commands."""

import argparse
import json
import sys
from dataclasses import dataclass

from .database import Database
from .errors import ProgressError
from .projects import ProjectStore
from .reads import DEFAULT_LIMIT, MAX_LIMIT, ReadStore
from .render import render
from .writes import WriteStore


class CliUsageError(Exception):
	"""Signal a parser error to format through the stable JSON envelope."""


class ProgressArgumentParser(argparse.ArgumentParser):
	"""Raise CliUsageError on a bad argument instead of exiting the process directly."""

	def error(self, message: str) -> None:
		"""Turn an argparse usage error into a CliUsageError the caller can format."""
		raise CliUsageError(message)


@dataclass(frozen=True)
class _ArgumentSpec:
	"""Describe one argument on a command parser."""

	names: tuple[str, ...]
	kwargs: dict[str, object]


@dataclass(frozen=True)
class _CommandSpec:
	"""Describe one command parser and any nested command parsers."""

	name: str
	help_text: str
	arguments: tuple[_ArgumentSpec, ...] = ()
	children: tuple["_CommandSpec", ...] = ()
	destination: str | None = None
	page_options: bool = False


def _argument(*names: str, **kwargs: object) -> _ArgumentSpec:
	"""Create one declarative argparse argument specification."""
	return _ArgumentSpec(names, kwargs)


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


def _add_command_specs(
	commands: argparse._SubParsersAction,
	specs: tuple[_CommandSpec, ...],
) -> None:
	"""Build command parsers from the shared declarative command specification."""
	for spec in specs:
		parser = commands.add_parser(spec.name, help=spec.help_text)

		if spec.children:
			nested_commands = parser.add_subparsers(
				dest=spec.destination, required=True
			)
			_add_command_specs(nested_commands, spec.children)
			continue

		for argument in spec.arguments:
			argument_kwargs = dict(argument.kwargs)
			if isinstance(argument_kwargs.get("default"), list):
				argument_kwargs["default"] = list(argument_kwargs["default"])

			parser.add_argument(*argument.names, **argument_kwargs)

		if spec.page_options:
			_add_page_options(parser)

		_add_output_options(parser)


# Every CLI command and its nested subcommands, in the order they should appear in --help.
_COMMAND_SPECS = (
	_CommandSpec("next", "show the current task and active chunk"),
	_CommandSpec("current", "show the current task and active chunk"),
	_CommandSpec(
		"project",
		"manage the current project binding",
		children=(
			_CommandSpec(
				"init",
				"create and bind a project",
				arguments=(
					_argument("--slug", required=True),
					_argument("--name", required=True),
				),
			),
			_CommandSpec(
				"attach",
				"bind an existing project",
				arguments=(_argument("project_id"),),
			),
			_CommandSpec("current", "show the bound project"),
		),
		destination="project_command",
	),
	_CommandSpec(
		"release",
		"read release records",
		children=(
			_CommandSpec(
				"add",
				"create a release",
				arguments=(
					_argument("--slug", required=True),
					_argument("--title", required=True),
					_argument("--overview", default=""),
					_argument(
						"--status",
						choices=("planned", "active", "done"),
						default="planned",
					),
					_argument("--position", type=int),
				),
			),
			_CommandSpec("list", "list releases", page_options=True),
			_CommandSpec(
				"remove",
				"remove a release",
				arguments=(_argument("release_id"),),
			),
			_CommandSpec(
				"rename",
				"rename a release",
				arguments=(
					_argument("release_id"),
					_argument("--title", required=True),
				),
			),
			_CommandSpec(
				"complete",
				"complete a planned or active release",
				arguments=(_argument("release_id"),),
			),
		),
		destination="release_command",
	),
	_CommandSpec(
		"task",
		"read task records",
		children=(
			_CommandSpec(
				"add",
				"create a task",
				arguments=(
					_argument("--slug", required=True),
					_argument("--title", required=True),
					_argument("--overview", default=""),
					_argument("--purpose", default=""),
					_argument("--contract", default=""),
					_argument("--model-tier", default=None),
					_argument("--files", default=None),
					_argument("--acceptance-criteria", default=""),
					_argument("--verification", default=""),
					_argument("--risks", default=""),
					_argument("--release", "--release-id", dest="release_id"),
					_argument(
						"--depends-on",
						"--dependency",
						dest="depends_on",
						action="append",
						default=[],
					),
					_argument("--position", type=int),
				),
			),
			_CommandSpec(
				"dependency",
				"manage task dependencies",
				children=(
					_CommandSpec(
						"add",
						"add a task dependency",
						arguments=(
							_argument("task_id"),
							_argument("depends_on_task_id"),
						),
					),
					_CommandSpec(
						"remove",
						"remove a task dependency",
						arguments=(
							_argument("task_id"),
							_argument("depends_on_task_id"),
						),
					),
				),
				destination="dependency_command",
			),
			_CommandSpec(
				"remove",
				"remove a task",
				arguments=(_argument("task_id"),),
			),
			_CommandSpec(
				"rename",
				"rename a task",
				arguments=(_argument("task_id"), _argument("--title", required=True)),
			),
			_CommandSpec(
				"start",
				"start a ready task",
				arguments=(_argument("task_id"),),
			),
			_CommandSpec(
				"complete",
				"complete a task",
				arguments=(_argument("task_id"),),
			),
			_CommandSpec(
				"block",
				"block a task",
				arguments=(
					_argument("task_id"),
					_argument("--reason", required=True),
					_argument("--needs-decision", action="store_true"),
				),
			),
			_CommandSpec(
				"unblock",
				"make a blocked task ready",
				arguments=(_argument("task_id"),),
			),
			_CommandSpec(
				"get",
				"show one task",
				arguments=(_argument("task_id"),),
			),
			_CommandSpec(
				"list",
				"list tasks",
				arguments=(_argument("--status"),),
				page_options=True,
			),
		),
		destination="task_command",
	),
	_CommandSpec(
		"chunk",
		"read chunk records",
		children=(
			_CommandSpec(
				"add",
				"create a pending chunk",
				arguments=(
					_argument("--task", required=True, dest="task_id"),
					_argument("--title", required=True),
					_argument("--description", default=""),
					_argument("--position", type=int),
				),
			),
			_CommandSpec(
				"complete",
				"complete an active chunk",
				arguments=(_argument("chunk_id"),),
			),
			_CommandSpec(
				"remove",
				"remove a chunk",
				arguments=(_argument("chunk_id"),),
			),
			_CommandSpec(
				"rename",
				"rename a chunk",
				arguments=(_argument("chunk_id"), _argument("--title", required=True)),
			),
			_CommandSpec(
				"list",
				"list chunks for a task",
				arguments=(_argument("--task", required=True, dest="task_id"),),
				page_options=True,
			),
		),
		destination="chunk_command",
	),
	_CommandSpec("ready", "list tasks ready to start", page_options=True),
	_CommandSpec(
		"discovery",
		"record a discovery note",
		children=(
			_CommandSpec(
				"add",
				"add a discovery note",
				arguments=(
					_argument("--task", required=True, dest="task_id"),
					_argument("body", nargs="+"),
				),
			),
			_CommandSpec(
				"remove",
				"remove a discovery note",
				arguments=(_argument("note_id"),),
			),
		),
		destination="discovery_command",
	),
	_CommandSpec(
		"decision",
		"record a decision note",
		children=(
			_CommandSpec(
				"add",
				"add a decision note",
				arguments=(
					_argument("--task", required=True, dest="task_id"),
					_argument("--supersedes", dest="supersedes_id"),
					_argument("body", nargs="+"),
				),
			),
			_CommandSpec(
				"remove",
				"remove a decision note",
				arguments=(_argument("note_id"),),
			),
		),
		destination="decision_command",
	),
	_CommandSpec(
		"context",
		"replace handoff context",
		children=(
			_CommandSpec(
				"set",
				"set current handoff context",
				arguments=(
					_argument("--current-goal"),
					_argument("--previous-step"),
					_argument("--next-step"),
					_argument("--standing-context"),
					_argument("--verify-with"),
					_argument("--stop-marker"),
				),
			),
		),
		destination="context_command",
	),
)


def build_parser() -> argparse.ArgumentParser:
	"""Build the full progress command parser with read and write subcommands."""
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
	commands = parser.add_subparsers(dest="command")

	_add_command_specs(commands, _COMMAND_SPECS)

	return parser


def main(argv: list[str] | None = None) -> int:
	"""Parse arguments, run the matching command, and return the process exit status."""
	arguments = list(sys.argv[1:] if argv is None else argv)
	json_mode = "--json" in arguments

	try:
		parser = build_parser()
		args = parser.parse_args(arguments)
		json_mode = bool(getattr(args, "json", json_mode))

		if args.command is None:
			parser.print_help()
			return 0

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
		hint = _human_hint(command, data)
		if hint:
			sys.stdout.write(f"Next: {hint}\n")

	return 0


def _run_command(args: argparse.Namespace) -> tuple[object, str]:
	"""Dispatch parsed arguments to the matching read or write store call."""
	database = Database(getattr(args, "database", None))
	command_name = args.command
	subcommand_name = getattr(args, f"{command_name}_command", None)
	dispatch = {
		("next", None): lambda: (ReadStore(database).next(), "next"),
		("current", None): lambda: (ReadStore(database).current(), "current"),
		("project", "init"): lambda: (_run_project(args, database), "project init"),
		("project", "attach"): lambda: (_run_project(args, database), "project attach"),
		("project", "current"): lambda: (
			_run_project(args, database),
			"project current",
		),
		("release", "add"): lambda: (
			WriteStore(database).release_add(
				slug=args.slug,
				title=args.title,
				overview=args.overview,
				status=args.status,
				position=args.position,
			),
			"release add",
		),
		("release", "list"): lambda: (
			ReadStore(database).release_list(args.limit, args.offset),
			"release list",
		),
		("release", "remove"): lambda: (
			WriteStore(database).release_remove(args.release_id),
			"release remove",
		),
		("release", "rename"): lambda: (
			WriteStore(database).release_rename(args.release_id, args.title),
			"release rename",
		),
		("release", "complete"): lambda: (
			WriteStore(database).release_complete(args.release_id),
			"release complete",
		),
		("task", "add"): lambda: (
			WriteStore(database).task_add(
				slug=args.slug,
				title=args.title,
				overview=args.overview,
				purpose=args.purpose,
				contract=args.contract,
				model_tier=args.model_tier,
				files=args.files,
				acceptance_criteria=args.acceptance_criteria,
				verification=args.verification,
				risks=args.risks,
				release_id=args.release_id,
				depends_on=args.depends_on,
				position=args.position,
			),
			"task add",
		),
		("task", "dependency"): lambda: _run_task_dependency(args, database),
		("task", "remove"): lambda: (
			WriteStore(database).task_remove(args.task_id),
			"task remove",
		),
		("task", "rename"): lambda: (
			WriteStore(database).task_rename(args.task_id, args.title),
			"task rename",
		),
		("task", "start"): lambda: (
			WriteStore(database).task_start(args.task_id),
			"task start",
		),
		("task", "complete"): lambda: (
			WriteStore(database).task_complete(args.task_id),
			"task complete",
		),
		("task", "block"): lambda: (
			WriteStore(database).task_block(
				args.task_id, args.reason, args.needs_decision
			),
			"task block",
		),
		("task", "unblock"): lambda: (
			WriteStore(database).task_unblock(args.task_id),
			"task unblock",
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
		("chunk", "add"): lambda: (
			WriteStore(database).chunk_add(
				task_id=args.task_id,
				title=args.title,
				description=args.description,
				position=args.position,
			),
			"chunk add",
		),
		("chunk", "complete"): lambda: (
			WriteStore(database).chunk_complete(args.chunk_id),
			"chunk complete",
		),
		("chunk", "remove"): lambda: (
			WriteStore(database).chunk_remove(args.chunk_id),
			"chunk remove",
		),
		("chunk", "rename"): lambda: (
			WriteStore(database).chunk_rename(args.chunk_id, args.title),
			"chunk rename",
		),
		("ready", None): lambda: (
			ReadStore(database).ready(args.limit, args.offset),
			"ready",
		),
		("discovery", "add"): lambda: (
			WriteStore(database).discovery_add(args.task_id, " ".join(args.body)),
			"discovery add",
		),
		("discovery", "remove"): lambda: (
			WriteStore(database).discovery_remove(args.note_id),
			"discovery remove",
		),
		("decision", "add"): lambda: (
			WriteStore(database).decision_add(
				args.task_id, " ".join(args.body), args.supersedes_id
			),
			"decision add",
		),
		("decision", "remove"): lambda: (
			WriteStore(database).decision_remove(args.note_id),
			"decision remove",
		),
		("context", "set"): lambda: (
			WriteStore(database).context_set(
				current_goal=args.current_goal,
				previous_step=args.previous_step,
				next_step=args.next_step,
				standing_context=args.standing_context,
				verify_with=args.verify_with,
				stop_marker=args.stop_marker,
			),
			"context set",
		),
	}
	handler = dispatch.get((command_name, subcommand_name))
	if handler is None:
		raise CliUsageError("unknown progress command")

	return handler()


def _run_task_dependency(
	args: argparse.Namespace, database: Database
) -> tuple[object, str]:
	"""Run the nested task dependency command."""
	if args.dependency_command == "remove":
		return (
			WriteStore(database).task_dependency_remove(
				args.task_id, args.depends_on_task_id
			),
			"task dependency remove",
		)

	return (
		WriteStore(database).task_dependency_add(args.task_id, args.depends_on_task_id),
		"task dependency add",
	)


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


def _human_hint(command: str, data: object) -> str | None:
	"""Return one useful follow-up command without changing the JSON contract."""
	if not isinstance(data, dict):
		return None

	object_id = data.get("id")
	task_id = data.get("task_id")
	hints = {
		"release add": (
			f"progress task add --release {object_id} --help" if object_id else None
		),
		"task add": (
			f"progress chunk add --task {object_id} --help" if object_id else None
		),
		"task dependency add": (
			f"progress task start {object_id}" if object_id else None
		),
		"task start": "progress next" if object_id else None,
		"task complete": "progress ready",
		"task block": (f"progress task unblock {object_id}" if object_id else None),
		"task unblock": (f"progress task start {object_id}" if object_id else None),
		"chunk add": f"progress task start {task_id}" if task_id else None,
		"chunk complete": "progress next",
		"discovery add": "progress next",
		"decision add": "progress next",
		"context set": "progress next",
	}
	return hints.get(command)


if __name__ == "__main__":
	raise SystemExit(main())
