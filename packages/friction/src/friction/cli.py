"""Command-line interface for recording and inspecting friction data."""

import argparse
import json
import sys
from datetime import datetime

from .database import Database
from .errors import DatabaseBusyError
from .hooks import record_claude_tool_failure
from .imports import import_files
from .reads import list_events, list_keys, suggest_keys, summary
from .writes import CATEGORIES, add_event, add_resolution, apply_keys, database_doctor
from . import style


class CliUsageError(Exception):
	"""Signal a command usage problem that main can format consistently."""


class FrictionArgumentParser(argparse.ArgumentParser):
	"""Raise usage errors so human and JSON modes share one exit path."""

	def error(self, message: str) -> None:
		"""Raise a formatting-friendly exception instead of exiting immediately."""
		raise CliUsageError(message)


def _validate_date(value: str) -> str:
	"""Check a --since/--until value is a real date before it is compared as text.

	Returned unchanged so the caller compares it against timestamp_utc directly.
	"""
	try:
		parsed = datetime.strptime(value, "%Y-%m-%d")
	except ValueError as error:
		raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error

	# strptime accepts unpadded fields like 2026-9-3; require the canonical form.
	if parsed.strftime("%Y-%m-%d") != value:
		raise argparse.ArgumentTypeError("expected YYYY-MM-DD")

	return value


def _add_output_options(parser: argparse.ArgumentParser) -> None:
	"""Add the database selector and JSON flag accepted by each command."""
	parser.add_argument(
		"--database",
		metavar="PATH",
		default=argparse.SUPPRESS,
		help="use PATH instead of $FRICTION_DATABASE or ~/.agents/friction.db",
	)
	parser.add_argument(
		"--json",
		action="store_true",
		default=argparse.SUPPRESS,
		help="write a JSON result for automation",
	)


def build_parser() -> argparse.ArgumentParser:
	"""Build the friction parser and its currently supported commands."""
	parser = FrictionArgumentParser(
		prog="friction",
		description="Record and inspect local agent friction events.",
	)
	parser.add_argument(
		"--database",
		metavar="PATH",
		default=None,
		help="use PATH instead of $FRICTION_DATABASE or ~/.agents/friction.db",
	)
	parser.add_argument(
		"--json",
		action="store_true",
		default=False,
		help="write a JSON result for automation",
	)
	commands = parser.add_subparsers(dest="command", metavar="COMMAND")

	add_parser = commands.add_parser("add", help="record a manual friction event")
	add_parser.add_argument("category", choices=sorted(CATEGORIES))
	add_parser.add_argument("detail")
	add_parser.add_argument(
		"--key", metavar="KEY", help="optional lowercase kebab-case pattern key"
	)
	_add_output_options(add_parser)

	summary_parser = commands.add_parser(
		"summary",
		help="show active friction grouped by category and pattern",
	)
	summary_parser.add_argument(
		"--category",
		action="append",
		choices=sorted(CATEGORIES),
		default=[],
		help="include only one category; repeat to select several",
	)
	summary_parser.add_argument(
		"--include-check-fails",
		action="store_true",
		help="include automated check failures",
	)
	summary_parser.add_argument(
		"--include-tool-errors",
		action="store_true",
		help="include automated tool errors",
	)
	_add_output_options(summary_parser)

	list_parser = commands.add_parser("list", help="show individual friction events")
	list_parser.add_argument(
		"--category",
		action="append",
		choices=sorted(CATEGORIES),
		default=[],
		help="include only one category; repeat to select several",
	)
	list_parser.add_argument(
		"--include-check-fails",
		action="store_true",
		help="include automated check failures",
	)
	list_parser.add_argument(
		"--include-tool-errors",
		action="store_true",
		help="include automated tool errors",
	)
	list_parser.add_argument(
		"--since",
		type=_validate_date,
		metavar="YYYY-MM-DD",
		help="include events on or after this date",
	)
	list_parser.add_argument(
		"--until",
		type=_validate_date,
		metavar="YYYY-MM-DD",
		help="include events on or before this date",
	)
	_add_output_options(list_parser)

	keys_parser = commands.add_parser(
		"keys", help="show stored pattern keys with their entry counts"
	)
	_add_output_options(keys_parser)

	import_parser = commands.add_parser(
		"import", help="import legacy friction TSV files"
	)
	import_parser.add_argument("paths", nargs="+", metavar="PATH")
	import_parser.add_argument(
		"--dry-run", action="store_true", help="report rows without writing them"
	)
	_add_output_options(import_parser)

	apply_keys_parser = commands.add_parser(
		"apply-keys", help="set entry keys from a JSON file of entry IDs to keys"
	)
	apply_keys_parser.add_argument(
		"file", metavar="FILE", help="JSON object of entry IDs to keys"
	)
	_add_output_options(apply_keys_parser)

	hook_parser = commands.add_parser("hook", help="run an integration hook")
	hook_commands = hook_parser.add_subparsers(dest="hook_command", required=True)
	claude_hook_parser = hook_commands.add_parser(
		"claude-tool-failure", help="record one Claude tool failure"
	)
	_add_output_options(claude_hook_parser)

	resolve_parser = commands.add_parser(
		"resolve",
		help="record a resolution for a category and pattern",
	)
	resolve_parser.add_argument("category", choices=sorted(CATEGORIES))
	resolve_parser.add_argument(
		"pattern",
		nargs="?",
		metavar="DETAIL",
		help="exact detail of an unkeyed pattern",
	)
	resolve_parser.add_argument(
		"--key", metavar="KEY", help="resolve a lowercase kebab-case pattern key"
	)
	resolve_parser.add_argument("--reference", required=True, metavar="REF")
	_add_output_options(resolve_parser)

	doctor_parser = commands.add_parser("doctor", help="check database health")
	_add_output_options(doctor_parser)

	return parser


def _write_json(payload: dict[str, object]) -> None:
	"""Write one compact JSON response to standard output."""
	sys.stdout.write(json.dumps(payload) + "\n")


def _write_error(message: str, json_mode: bool, status_code: int) -> int:
	"""Write a command error in the selected output mode and return its status."""
	if json_mode:
		_write_json({"ok": False, "error": {"message": message}})
	else:
		print(style.status("failed", "Error", message), file=sys.stderr)

	return status_code


def _human_output(
	command: str,
	data: dict[str, object] | list[dict[str, object]],
) -> str:
	"""Format the human result for one completed command."""
	if command == "add":
		logged = style.span(f"Logged: {data['category']} — {data['detail']}", "success")
		if data["suggested_keys"]:
			return f"{logged}\nSimilar keys: {', '.join(data['suggested_keys'])}"

		return logged

	if command == "resolve":
		return style.span(
			f"Resolved: {data['category']} — {data['pattern']}",
			"success",
		)

	if command == "summary":
		if not data:
			return style.span("No active friction entries.")

		lines = []
		for item in data:
			row = style.row(str(item["count"]), _summary_row_text(item))
			folders = ", ".join(item["folders"])
			lines.append(f"{row}\n  folders: {folders}")

		return "\n".join(lines)

	if command == "list":
		if not data:
			return style.span("No friction entries.")

		return style.table(
			[
				{"key": "id", "label": "ID"},
				{"key": "timestamp", "label": "Timestamp"},
				{"key": "category", "label": "Category"},
				{"key": "pattern_key", "label": "Key"},
				{
					"key": "cwd",
					"label": "CWD",
					"maxWidth": 40,
					"overflow": "wrap",
				},
				{
					"key": "detail",
					"label": "Detail",
					"maxWidth": 60,
					"overflow": "wrap",
				},
			],
			[
				{
					"id": str(item["id"]),
					"timestamp": str(item["timestamp_utc"]),
					"category": str(item["category"]),
					"pattern_key": str(item["key"] or ""),
					"cwd": str(item["cwd"]),
					"detail": str(item["detail"]),
				}
				for item in data
			],
		)

	if command == "keys":
		if not data:
			return style.span("No keys recorded.")

		return style.table(
			[
				{"key": "key", "label": "Key"},
				{"key": "category", "label": "Category"},
				{"key": "count", "label": "Entries"},
			],
			[
				{
					"key": str(item["key"]),
					"category": str(item["category"]),
					"count": str(item["count"]),
				}
				for item in data
			],
		)

	if command == "import":
		rejected_rows = data["rejected_rows"]
		rows = [
			style.row(
				f"{item['source_path']}:{item['source_line']}", item["reason"], "failed"
			)
			for item in rejected_rows
		]
		rows.extend(
			(
				style.row("Imported", str(data["imported"])),
				style.row("Skipped duplicates", str(data["skipped_as_duplicate"])),
				style.row("Rejected", str(data["rejected"])),
			)
		)
		return "\n".join(rows)

	if command == "apply-keys":
		return style.row("Keys changed", str(data["changed"]))

	return "\n".join(
		(
			style.row("Database", str(data["database_path"])),
			style.row("Schema version", str(data["schema_version"])),
			style.row("Database writable", str(data["database_writable"])),
			style.row("Directory writable", str(data["directory_writable"])),
			style.row("Integrity", str(data["integrity_check"])),
		)
	)


def _summary_row_text(item: dict[str, object]) -> str:
	"""Describe one pattern after its count in the human summary."""
	if item["key"] is not None:
		identity = item["key"]
	elif item["category"] == "tool-error":
		identity = f"{item['command_family']}  {item['error_class']}"
	else:
		identity = ""

	parts = [str(item["category"]), str(identity), f"(latest: {item['example']})"]
	return "  ".join(part for part in parts if part)


def _run_command(
	args: argparse.Namespace,
) -> tuple[dict[str, object] | list[dict[str, object]], str]:
	"""Run the selected command and return its data and command name."""
	database = Database(getattr(args, "database", None))
	if args.command == "add":
		data = add_event(database, args.category, args.detail, args.key)
		data["suggested_keys"] = (
			suggest_keys(database, args.key, args.category)
			if args.key is not None
			else []
		)
		return data, "add"

	if args.command == "summary":
		return (
			summary(
				database,
				args.category,
				include_check_fails=args.include_check_fails,
				include_tool_errors=args.include_tool_errors,
			),
			"summary",
		)

	if args.command == "list":
		return (
			list_events(
				database,
				args.category,
				include_check_fails=args.include_check_fails,
				include_tool_errors=args.include_tool_errors,
				since=args.since,
				until=args.until,
			),
			"list",
		)

	if args.command == "keys":
		return list_keys(database), "keys"

	if args.command == "import":
		return import_files(database, args.paths, dry_run=args.dry_run), "import"
	if args.command == "apply-keys":
		return apply_keys(database, args.file), "apply-keys"
	if args.command == "hook" and args.hook_command == "claude-tool-failure":
		try:
			raw_input = sys.stdin.read()
		except Exception:
			return {}, "hook"

		record_claude_tool_failure(database, raw_input)
		return {}, "hook"

	if args.command == "resolve":
		if (args.pattern is None) == (args.key is None):
			raise CliUsageError("provide exactly one of DETAIL or --key KEY")

		pattern = args.key if args.key is not None else args.pattern
		target = "key" if args.key is not None else "detail"
		return (
			add_resolution(database, args.category, pattern, args.reference, target),
			"resolve",
		)

	if args.command == "doctor":
		return database_doctor(database), "doctor"

	raise CliUsageError("a command is required")


def main(argv: list[str] | None = None) -> int:
	"""Parse arguments, run a command, and return a shell exit status."""
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
		return _write_error(str(error), json_mode, 2)
	except (DatabaseBusyError, OSError, ValueError) as error:
		return _write_error(str(error), json_mode, 1)

	if command == "hook":
		return 0
	if json_mode:
		_write_json({"ok": True, "data": data})
	else:
		print(_human_output(command, data))

	return 0
