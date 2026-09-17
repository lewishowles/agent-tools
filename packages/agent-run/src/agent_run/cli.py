"""Command-line entry point for agent-run."""

import argparse
import json
import signal
import sqlite3
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

from agent_run.commands import (
    Command,
    CommandError,
    CommandNotFoundError,
    add_command,
    edit_command,
    list_commands,
    normalise_working_directory,
    remove_command,
    rename_command,
)
from agent_run.database import connect_database, resolve_database_path
from agent_run.execution import RunResult, run_command
from agent_run.output import render_error, render_success
from agent_run.repository import RepositoryError, identify_repository
from agent_run.runs import RunRecord, create_run_log, discard_run_log, save_run
from agent_run.schema import NewerSchemaError


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports mistakes in the same format as other errors.

    Plain argparse prints its own message and exits, which would break the JSON
    envelope when `--json` is set. The caller determines the mode from the raw
    arguments before parsing and passes it to each parser instance.
    """

    def __init__(
        self, *args: object, json_mode: bool = False, **kwargs: object
    ) -> None:
        """Create a parser with its output mode known before parsing errors."""
        super().__init__(*args, **kwargs)
        self._json_mode = json_mode

    def error(self, message: str) -> NoReturn:
        """Report a usage error and exit with the `usage` status.

        Args:
            message: argparse's description of what was wrong.
        """
        diagnostic = f"{self.format_usage().strip()}\n{self.prog}: error: {message}"
        exit_code = render_error(
            json_mode=self._json_mode,
            code="usage",
            message=message,
            text=diagnostic,
            diagnostic=diagnostic,
        )
        raise SystemExit(exit_code)


def main(argv: Sequence[str] | None = None) -> int:
    """Run agent-run and return its exit status.

    Bare calls show help. The repository command identifies the local Git
    repository and returns its root and stable ID. Invalid arguments exit
    through `_ArgumentParser.error` instead of returning.

    Args:
        argv: Arguments without the program name; defaults to `sys.argv[1:]`.
    """
    values = list(sys.argv[1:] if argv is None else argv)

    # Anything after `--` belongs to the registered command, so a `--json` there
    # is a command argument rather than a request for JSON output.
    separator_index = values.index("--") if "--" in values else len(values)
    json_mode = "--json" in values[:separator_index]

    parser = _ArgumentParser(
        prog="agent-run",
        description="Run project commands with bounded evidence.",
        add_help=False,
        json_mode=json_mode,
    )
    parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        help="Show this help message and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write one structured JSON result to standard output.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    repository_parser = subparsers.add_parser(
        "repository",
        help="Identify the local Git repository.",
        description="Identify the local Git repository.",
        add_help=False,
        json_mode=json_mode,
    )
    repository_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="repository_help",
        help="Show this help message and exit.",
    )
    repository_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )

    add_parser = subparsers.add_parser(
        "add",
        help="Register a named project command.",
        description="Register a named project command.",
        usage="agent-run add NAME [--cwd DIR] [--json] -- ARGV...",
        add_help=False,
        json_mode=json_mode,
    )
    add_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="add_help",
        help="Show this help message and exit.",
    )
    add_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    add_parser.add_argument(
        "--cwd",
        metavar="DIR",
        default=None,
        help=(
            "Run from DIR relative to the repository root (default: root); "
            "symlinked directories are stored as their resolved target."
        ),
    )
    add_parser.add_argument("name", nargs="?", help="Name used to run the command.")

    edit_parser = subparsers.add_parser(
        "edit",
        help="Change a named project command.",
        description="Change a named project command.",
        usage="agent-run edit NAME [--cwd DIR] [--json] [-- ARGV...]",
        add_help=False,
        json_mode=json_mode,
    )
    edit_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="edit_help",
        help="Show this help message and exit.",
    )
    edit_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    edit_parser.add_argument(
        "--cwd",
        metavar="DIR",
        default=None,
        help=(
            "Change the working directory to DIR relative to the repository root; "
            "symlinked directories are stored as their resolved target."
        ),
    )
    edit_parser.add_argument("name", nargs="?", help="Name of the command to change.")

    run_parser = subparsers.add_parser(
        "run",
        help="Run a direct project command.",
        description="Run a direct project command in the foreground.",
        usage="agent-run run [--cwd DIR] --timeout SECONDS [--json] -- ARGV...",
        add_help=False,
        json_mode=json_mode,
    )
    run_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="run_help",
        help="Show this help message and exit.",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    run_parser.add_argument(
        "--cwd",
        metavar="DIR",
        default=None,
        help=(
            "Run from DIR relative to the repository root (default: root); "
            "symlinked directories are resolved before execution."
        ),
    )
    run_parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help="Stop the command after this positive number of seconds.",
    )

    rename_parser = subparsers.add_parser(
        "rename",
        help="Rename a named project command.",
        description="Rename a named project command.",
        usage="agent-run rename NAME NEW_NAME [--json]",
        add_help=False,
        json_mode=json_mode,
    )
    rename_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="rename_help",
        help="Show this help message and exit.",
    )
    rename_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    rename_parser.add_argument("name", nargs="?", help="Current name of the command.")
    rename_parser.add_argument(
        "new_name", nargs="?", help="Replacement name for the command."
    )

    remove_parser = subparsers.add_parser(
        "remove",
        help="Remove a named project command.",
        description="Remove a named project command.",
        usage="agent-run remove NAME [--json]",
        add_help=False,
        json_mode=json_mode,
    )
    remove_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="remove_help",
        help="Show this help message and exit.",
    )
    remove_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    remove_parser.add_argument("name", nargs="?", help="Name of the command to remove.")

    list_parser = subparsers.add_parser(
        "list",
        help="List named project commands.",
        description="List named project commands in name order.",
        add_help=False,
        json_mode=json_mode,
    )
    list_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="list_help",
        help="Show this help message and exit.",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )

    # Only the part before `--` is parsed, so argparse never reads stored command
    # arguments. Leftovers are reported here so add can give a message that
    # names the separator.
    parsed, unknown = parser.parse_known_args(values[:separator_index])

    if unknown:
        if parsed.command in {"add", "edit", "run"}:
            command_parser = {
                "add": add_parser,
                "edit": edit_parser,
                "run": run_parser,
            }[parsed.command]
            command_parser.error("all command arguments must follow --")

        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    if parsed.command not in {"add", "edit", "run"} and separator_index < len(values):
        parser.error(
            "the -- separator is only valid for the add, edit, or run commands"
        )

    if parsed.command == "repository" and parsed.repository_help:
        repository_help_text = repository_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": repository_help_text})

        return render_success(json_mode=False, text=repository_help_text)

    if parsed.command == "add" and parsed.add_help:
        add_help_text = add_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": add_help_text})

        return render_success(json_mode=False, text=add_help_text)

    if parsed.command == "edit" and parsed.edit_help:
        edit_help_text = edit_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": edit_help_text})

        return render_success(json_mode=False, text=edit_help_text)

    if parsed.command == "run" and parsed.run_help:
        run_help_text = run_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": run_help_text})

        return render_success(json_mode=False, text=run_help_text)

    if parsed.command == "rename" and parsed.rename_help:
        rename_help_text = rename_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": rename_help_text})

        return render_success(json_mode=False, text=rename_help_text)

    if parsed.command == "remove" and parsed.remove_help:
        remove_help_text = remove_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": remove_help_text})

        return render_success(json_mode=False, text=remove_help_text)

    if parsed.command == "list" and parsed.list_help:
        list_help_text = list_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": list_help_text})

        return render_success(json_mode=False, text=list_help_text)

    if parsed.help or parsed.command is None:
        help_text = parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": help_text})

        return render_success(json_mode=False, text=help_text)

    if parsed.command == "repository":
        try:
            repository = identify_repository()
        except RepositoryError as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )

        data = {"root": str(repository.root), "id": repository.id}
        text = f"root: {repository.root}\nid: {repository.id}"

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "run":
        # The timeout is optional to argparse so `run --help` reaches the help
        # branch above; it is still required to execute a command.
        if parsed.timeout is None:
            run_parser.error("the following arguments are required: --timeout")

        command_arguments = values[separator_index + 1 :]

        if not command_arguments:
            run_parser.error("the following arguments are required: ARGV")

        connection: sqlite3.Connection | None = None
        log_path: Path | None = None

        try:
            repository = identify_repository()
            relative_working_directory = normalise_working_directory(
                repository, parsed.cwd
            )
            database_path = resolve_database_path()
            connection = connect_database(database_path)
            run_id, log_path = create_run_log(database_path)
            started_at = datetime.now(timezone.utc).isoformat()
            started_monotonic = time.monotonic()
            interrupted = False

            try:
                result = run_command(
                    command_arguments,
                    repository.root / relative_working_directory,
                    parsed.timeout,
                    log_path,
                )
            except KeyboardInterrupt:
                interrupted = True
                result = RunResult(
                    argv=tuple(command_arguments),
                    working_directory=(
                        repository.root / relative_working_directory
                    ).resolve(),
                    exit_status=-signal.SIGINT,
                    timed_out=False,
                    duration_seconds=time.monotonic() - started_monotonic,
                    log_path=log_path,
                )

            record = save_run(
                connection,
                run_id=run_id,
                repository_id=repository.id,
                argv=result.argv,
                working_directory=relative_working_directory,
                timeout_seconds=parsed.timeout,
                started_at=started_at,
                duration_seconds=result.duration_seconds,
                exit_status=result.exit_status,
                timed_out=result.timed_out,
                log_path=result.log_path,
            )
        except (RepositoryError, NewerSchemaError, OSError, sqlite3.Error) as error:
            if log_path is not None:
                discard_run_log(log_path)

            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except (CommandError, ValueError) as error:
            return render_error(
                json_mode=parsed.json,
                code="usage",
                message=str(error),
            )
        finally:
            if connection is not None:
                connection.close()

        failure_message = None

        if interrupted:
            failure_message = "Command interrupted."
        elif result.timed_out:
            unit = "second" if parsed.timeout == 1 else "seconds"
            failure_message = f"Command killed after {parsed.timeout:g} {unit}."
        elif result.exit_status != 0:
            failure_message = f"Command exited with status {result.exit_status}."

        if failure_message is None:
            data = _run_record(result, record)
            text = _format_run(result, record)

            if parsed.json:
                return render_success(json_mode=True, data=data)

            return render_success(json_mode=False, text=text)

        failure_message = (
            f"{failure_message} run ID: {record.run_id}; log path: {record.log_path}"
        )

        exit_code = render_error(
            json_mode=parsed.json,
            code="check-failed",
            message=failure_message,
            data=_run_record(result, record),
        )

        return 130 if interrupted else exit_code

    if parsed.command == "add":
        # The name is optional to argparse only so `add --help` reaches the help
        # branch above; it is still required to register a command.
        if parsed.name is None:
            add_parser.error("the following arguments are required: name")

        command_arguments = values[separator_index + 1 :]

        if not command_arguments:
            add_parser.error("the following arguments are required: ARGV")

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                command = add_command(
                    connection,
                    repository,
                    parsed.name,
                    command_arguments,
                    parsed.cwd,
                )
            finally:
                connection.close()
        except (RepositoryError, NewerSchemaError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except CommandError as error:
            return render_error(
                json_mode=parsed.json,
                code="usage",
                message=str(error),
            )

        data = _command_record(command)
        text = _format_command(command)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "edit":
        # The name is optional to argparse only so `edit --help` reaches the help
        # branch above; it is still required to change a command.
        if parsed.name is None:
            edit_parser.error("the following arguments are required: name")

        command_arguments = (
            values[separator_index + 1 :] if separator_index < len(values) else None
        )

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                command = edit_command(
                    connection,
                    repository,
                    parsed.name,
                    command_arguments,
                    parsed.cwd,
                )
            finally:
                connection.close()
        except (RepositoryError, NewerSchemaError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except CommandNotFoundError as error:
            return render_error(
                json_mode=parsed.json,
                code="not-found",
                message=str(error),
            )
        except CommandError as error:
            return render_error(
                json_mode=parsed.json,
                code="usage",
                message=str(error),
            )

        data = _command_record(command)
        text = _format_command(command)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "rename":
        # The names are optional to argparse only so `rename --help` reaches the
        # help branch above; both are required to rename a command.
        if parsed.name is None:
            rename_parser.error("the following arguments are required: name")

        if parsed.new_name is None:
            rename_parser.error("the following arguments are required: new_name")

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                command = rename_command(
                    connection,
                    repository,
                    parsed.name,
                    parsed.new_name,
                )
            finally:
                connection.close()
        except (RepositoryError, NewerSchemaError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except CommandNotFoundError as error:
            return render_error(
                json_mode=parsed.json,
                code="not-found",
                message=str(error),
            )
        except CommandError as error:
            return render_error(
                json_mode=parsed.json,
                code="usage",
                message=str(error),
            )

        data = _command_record(command)
        text = _format_command(command)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "remove":
        # The name is optional to argparse only so `remove --help` reaches the
        # help branch above; it is still required to remove a command.
        if parsed.name is None:
            remove_parser.error("the following arguments are required: name")

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                remove_command(connection, repository, parsed.name)
            finally:
                connection.close()
        except (RepositoryError, NewerSchemaError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except CommandNotFoundError as error:
            return render_error(
                json_mode=parsed.json,
                code="not-found",
                message=str(error),
            )

        data = {"name": parsed.name}
        text = f"name: {parsed.name}"

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "list":
        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                commands = list_commands(connection, repository)
            finally:
                connection.close()
        except (RepositoryError, NewerSchemaError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )

        data = {"commands": [_command_record(command) for command in commands]}
        text = _format_commands(commands)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)


def _format_command(command: Command) -> str:
    """Format one named command for human-readable output."""
    return "\n".join(
        [
            f"name: {command.name}",
            f"working directory: {command.working_directory}",
            f"argv: {json.dumps(list(command.argv))}",
        ]
    )


def _format_commands(commands: Sequence[Command]) -> str:
    """Format named commands as separated human-readable records."""
    if not commands:
        return "No named commands registered."

    return "\n\n".join(_format_command(command) for command in commands)


def _command_record(command: Command) -> dict[str, object]:
    """Return the public fields for one command result."""
    return {
        "name": command.name,
        "working_directory": command.working_directory,
        "argv": list(command.argv),
    }


def _run_record(result: RunResult, record: RunRecord) -> dict[str, object]:
    """Return the public fields for one direct command result."""
    return {
        "argv": list(result.argv),
        "working_directory": str(result.working_directory),
        "exit_status": result.exit_status,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
        "run_id": record.run_id,
        "log_path": str(record.log_path),
    }


def _format_run(result: RunResult, record: RunRecord) -> str:
    """Format one human-readable status line for a completed command."""
    return (
        f"exit status: {result.exit_status}; "
        f"duration: {result.duration_seconds:.3f}s; "
        f"run ID: {record.run_id}; log path: {record.log_path}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
