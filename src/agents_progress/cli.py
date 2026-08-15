"""Command-line interface for progress read and write commands."""

import argparse
import json
import sys

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


def _add_navigation_commands(commands: argparse._SubParsersAction) -> None:
	"""Add commands that show the current task and active chunk."""
	for name in ("next", "current"):
		command = commands.add_parser(
			name, help="show the current task and active chunk"
		)
		_add_output_options(command)


def _add_project_commands(commands: argparse._SubParsersAction) -> None:
	"""Add commands for managing the current project binding."""
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


def _add_release_commands(commands: argparse._SubParsersAction) -> None:
	"""Add release creation, lifecycle, and listing commands."""
	release = commands.add_parser("release", help="read release records")
	release_commands = release.add_subparsers(dest="release_command", required=True)
	release_add = release_commands.add_parser("add", help="create a release")
	release_add.add_argument("--slug", required=True)
	release_add.add_argument("--title", required=True)
	release_add.add_argument("--overview", default="")
	release_add.add_argument(
		"--status", choices=("planned", "active", "done"), default="planned"
	)
	release_add.add_argument("--position", type=int)
	_add_output_options(release_add)
	release_list = release_commands.add_parser("list", help="list releases")
	_add_page_options(release_list)
	_add_output_options(release_list)
	release_remove = release_commands.add_parser("remove", help="remove a release")
	release_remove.add_argument("release_id")
	_add_output_options(release_remove)


def _add_task_commands(commands: argparse._SubParsersAction) -> None:
	"""Add task creation, lifecycle, dependency, and query commands."""
	task = commands.add_parser("task", help="read task records")
	task_commands = task.add_subparsers(dest="task_command", required=True)
	task_add = task_commands.add_parser("add", help="create a task")
	task_add.add_argument("--slug", required=True)
	task_add.add_argument("--title", required=True)
	task_add.add_argument("--overview", default="")
	task_add.add_argument("--purpose", default="")
	task_add.add_argument("--contract", default="")
	task_add.add_argument("--model-tier", default=None)
	task_add.add_argument("--files", default=None)
	task_add.add_argument("--acceptance-criteria", default="")
	task_add.add_argument("--verification", default="")
	task_add.add_argument("--risks", default="")
	task_add.add_argument("--release", "--release-id", dest="release_id")
	task_add.add_argument(
		"--depends-on", "--dependency", dest="depends_on", action="append", default=[]
	)
	task_add.add_argument("--position", type=int)
	_add_output_options(task_add)
	task_dependency = task_commands.add_parser(
		"dependency", help="manage task dependencies"
	)
	task_dependency_commands = task_dependency.add_subparsers(
		dest="dependency_command", required=True
	)
	task_dependency_add = task_dependency_commands.add_parser(
		"add", help="add a task dependency"
	)
	task_dependency_add.add_argument("task_id")
	task_dependency_add.add_argument("depends_on_task_id")
	_add_output_options(task_dependency_add)
	task_dependency_remove = task_dependency_commands.add_parser(
		"remove", help="remove a task dependency"
	)
	task_dependency_remove.add_argument("task_id")
	task_dependency_remove.add_argument("depends_on_task_id")
	_add_output_options(task_dependency_remove)
	task_remove = task_commands.add_parser("remove", help="remove a task")
	task_remove.add_argument("task_id")
	_add_output_options(task_remove)
	task_start = task_commands.add_parser("start", help="start a ready task")
	task_start.add_argument("task_id")
	_add_output_options(task_start)
	task_complete = task_commands.add_parser("complete", help="complete a task")
	task_complete.add_argument("task_id")
	_add_output_options(task_complete)
	task_block = task_commands.add_parser("block", help="block a task")
	task_block.add_argument("task_id")
	task_block.add_argument("--reason", required=True)
	task_block.add_argument("--needs-decision", action="store_true")
	_add_output_options(task_block)
	task_unblock = task_commands.add_parser("unblock", help="make a blocked task ready")
	task_unblock.add_argument("task_id")
	_add_output_options(task_unblock)
	task_get = task_commands.add_parser("get", help="show one task")
	task_get.add_argument("task_id")
	_add_output_options(task_get)
	task_list = task_commands.add_parser("list", help="list tasks")
	task_list.add_argument("--status")
	_add_page_options(task_list)
	_add_output_options(task_list)


def _add_chunk_commands(commands: argparse._SubParsersAction) -> None:
	"""Add chunk creation, completion, and listing commands."""
	chunk = commands.add_parser("chunk", help="read chunk records")
	chunk_commands = chunk.add_subparsers(dest="chunk_command", required=True)
	chunk_add = chunk_commands.add_parser("add", help="create a pending chunk")
	chunk_add.add_argument("--task", required=True, dest="task_id")
	chunk_add.add_argument("--title", required=True)
	chunk_add.add_argument("--description", default="")
	chunk_add.add_argument("--position", type=int)
	_add_output_options(chunk_add)
	chunk_complete = chunk_commands.add_parser(
		"complete", help="complete an active chunk"
	)
	chunk_complete.add_argument("chunk_id")
	_add_output_options(chunk_complete)
	chunk_remove = chunk_commands.add_parser("remove", help="remove a chunk")
	chunk_remove.add_argument("chunk_id")
	_add_output_options(chunk_remove)
	chunk_list = chunk_commands.add_parser("list", help="list chunks for a task")
	chunk_list.add_argument("--task", required=True, dest="task_id")
	_add_page_options(chunk_list)
	_add_output_options(chunk_list)


def _add_ready_command(commands: argparse._SubParsersAction) -> None:
	"""Add the command that lists tasks ready to start."""
	ready = commands.add_parser("ready", help="list tasks ready to start")
	_add_page_options(ready)
	_add_output_options(ready)


def _add_discovery_commands(commands: argparse._SubParsersAction) -> None:
	"""Add the command for recording discovery notes."""
	discovery = commands.add_parser("discovery", help="record a discovery note")
	discovery_commands = discovery.add_subparsers(
		dest="discovery_command", required=True
	)
	discovery_add = discovery_commands.add_parser("add", help="add a discovery note")
	discovery_add.add_argument("--task", required=True, dest="task_id")
	discovery_add.add_argument("body", nargs="+")
	_add_output_options(discovery_add)
	discovery_remove = discovery_commands.add_parser(
		"remove", help="remove a discovery note"
	)
	discovery_remove.add_argument("note_id")
	_add_output_options(discovery_remove)


def _add_decision_commands(commands: argparse._SubParsersAction) -> None:
	"""Add the command for recording decision notes."""
	decision = commands.add_parser("decision", help="record a decision note")
	decision_commands = decision.add_subparsers(dest="decision_command", required=True)
	decision_add = decision_commands.add_parser("add", help="add a decision note")
	decision_add.add_argument("--task", required=True, dest="task_id")
	decision_add.add_argument("--supersedes", dest="supersedes_id")
	decision_add.add_argument("body", nargs="+")
	_add_output_options(decision_add)
	decision_remove = decision_commands.add_parser(
		"remove", help="remove a decision note"
	)
	decision_remove.add_argument("note_id")
	_add_output_options(decision_remove)


def _add_context_commands(commands: argparse._SubParsersAction) -> None:
	"""Add the command for replacing handoff context."""
	context = commands.add_parser("context", help="replace handoff context")
	context_commands = context.add_subparsers(dest="context_command", required=True)
	context_set = context_commands.add_parser("set", help="set current handoff context")
	context_set.add_argument("--current-goal")
	context_set.add_argument("--previous-step")
	context_set.add_argument("--next-step")
	context_set.add_argument("--standing-context")
	context_set.add_argument("--verify-with")
	context_set.add_argument("--stop-marker")
	_add_output_options(context_set)


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
	commands = parser.add_subparsers(dest="command", required=True)

	_add_navigation_commands(commands)
	_add_project_commands(commands)
	_add_release_commands(commands)
	_add_task_commands(commands)
	_add_chunk_commands(commands)
	_add_ready_command(commands)
	_add_discovery_commands(commands)
	_add_decision_commands(commands)
	_add_context_commands(commands)

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
