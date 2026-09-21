"""Command-line entry point for agent-run."""

import argparse
import json
import shlex
import signal
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import questionary

from agent_run.commands import (
    COMMAND_CAPABILITIES,
    DEFAULT_CAPABILITY,
    FILE_LIST_CAPABILITY,
    Command,
    CommandError,
    CommandNotFoundError,
    add_command,
    edit_command,
    find_command,
    list_commands,
    normalise_working_directory,
    remove_command,
    rename_command,
    starts_browser_runner,
)
from agent_run.database import connect_database, resolve_database_path
from agent_run.detectors import Candidate, collect_candidates
from agent_run.execution import DEFAULT_TIMEOUT_SECONDS, RunResult, run_command
from agent_run.failures import (
    Failure,
    FailureReport,
)
from agent_run.locking import RunBusyError, RunLock, acquire_run_lock
from agent_run.output import (
    render_command_result,
    render_empty_state,
    render_error,
    render_row_group,
    render_success,
)
from agent_run.readers import read_failure_report
from agent_run.repository import RepositoryError, identify_repository
from agent_run.retention import (
    PruneError,
    RetentionPlan,
    delete_prune_candidates,
    measure_log_sizes,
    select_prune_candidates,
)
from agent_run.runs import (
    DEFAULT_RUN_LIMIT,
    RunNotFoundError,
    RunRecord,
    create_run_log,
    discard_run_log,
    get_run,
    list_all_runs,
    list_runs,
    resolve_log_directory,
    save_run,
)
from agent_run.schema import NewerSchemaError
from agent_run.targets import resolve_file_targets

# How a detected command compares with the saved command of the same name.
CANDIDATE_NEW = "new"
CANDIDATE_SAVED = "saved"
CANDIDATE_CLASH = "clash"


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
        usage="agent-run add NAME [--cwd DIR] [--timeout SECONDS] [--capability CAPABILITY] [--manual] [--json] -- ARGV...",
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
    add_parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help=(
            f"Use this positive timeout for named runs "
            f"(default: {DEFAULT_TIMEOUT_SECONDS} seconds)."
        ),
    )
    add_parser.add_argument(
        "--capability",
        choices=COMMAND_CAPABILITIES,
        default=DEFAULT_CAPABILITY,
        help="Allow named runs to append file paths with `--file` or `--glob`.",
    )
    add_parser.add_argument(
        "--manual",
        action="store_true",
        help="Mark this command as manual-only; named runs will not execute it.",
    )
    add_parser.add_argument("name", nargs="?", help="Name used to run the command.")

    edit_parser = subparsers.add_parser(
        "edit",
        help="Change a named project command.",
        description="Change a named project command.",
        usage="agent-run edit NAME [--cwd DIR] [--timeout SECONDS] [--capability CAPABILITY] [--manual | --no-manual] [--json] [-- ARGV...]",
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
    edit_parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help="Change the timeout for named runs to this positive number of seconds.",
    )
    edit_parser.add_argument(
        "--capability",
        choices=COMMAND_CAPABILITIES,
        default=None,
        help="Change whether named runs may append file paths with `--file` or `--glob`.",
    )
    edit_parser.add_argument(
        "--manual",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Mark or unmark the command as manual-only.",
    )
    edit_parser.add_argument("name", nargs="?", help="Name of the command to change.")

    run_parser = subparsers.add_parser(
        "run",
        help="Run a named or direct project command.",
        description="Run a named or direct project command in the foreground.",
        usage="agent-run run [NAME] [--cwd DIR] [--timeout SECONDS] [--file PATH] [--glob PATTERN] [--json] [-- ARGV...]",
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
    run_parser.add_argument(
        "--file",
        metavar="PATH",
        action="append",
        default=[],
        help="Append PATH to a named command with the `file-list` capability.",
    )
    run_parser.add_argument(
        "--glob",
        metavar="PATTERN",
        action="append",
        default=[],
        help=(
            "Append sorted non-ignored files matching PATTERN from the repository "
            "root with the `file-list` capability."
        ),
    )
    run_parser.add_argument("name", nargs="?", help="Name of a stored command to run.")

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

    detect_parser = subparsers.add_parser(
        "detect",
        help="Preview detected project commands.",
        description="Preview detected project commands without registering them.",
        add_help=False,
        json_mode=json_mode,
    )
    detect_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="detect_help",
        help="Show this help message and exit.",
    )
    detect_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    detect_parser.add_argument(
        "--add",
        dest="add_names",
        action="append",
        nargs="?",
        default=None,
        metavar="NAME",
        help=(
            "Save a detected command by name; repeat for several commands, "
            "or omit NAME to choose interactively."
        ),
    )
    detect_parser.add_argument(
        "--all",
        dest="add_all",
        action="store_true",
        help="Save every detected command that is not already registered.",
    )

    runs_parser = subparsers.add_parser(
        "runs",
        help="List recent runs for the current repository.",
        description="List recent runs for the current repository.",
        add_help=False,
        json_mode=json_mode,
    )
    runs_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="runs_help",
        help="Show this help message and exit.",
    )
    runs_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    runs_parser.add_argument(
        "--limit",
        metavar="N",
        type=int,
        default=DEFAULT_RUN_LIMIT,
        help=(
            f"Show the N newest runs (default: {DEFAULT_RUN_LIMIT}); "
            "N must be positive."
        ),
    )

    prune_parser = subparsers.add_parser(
        "prune",
        help="Preview saved runs selected by log retention.",
        description=(
            "Preview saved runs selected by log retention; use --apply to delete them."
        ),
        usage="agent-run prune [--apply] [--json]",
        add_help=False,
        json_mode=json_mode,
    )
    prune_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="prune_help",
        help="Show this help message and exit.",
    )
    prune_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    prune_parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the selected runs and their logs.",
    )

    show_parser = subparsers.add_parser(
        "show",
        help="Show one saved run record.",
        description="Show one saved run record without rerunning it.",
        usage="agent-run show RUN_ID [--json]",
        add_help=False,
        json_mode=json_mode,
    )
    show_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="show_help",
        help="Show this help message and exit.",
    )
    show_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    show_parser.add_argument("run_id", nargs="?", help="ID of the saved run.")

    log_parser = subparsers.add_parser(
        "log",
        help="Print the complete log for one saved run.",
        description="Print the complete log for one saved run without rerunning it.",
        usage="agent-run log RUN_ID [--json]",
        add_help=False,
        json_mode=json_mode,
    )
    log_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="log_help",
        help="Show this help message and exit.",
    )
    log_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    log_parser.add_argument("run_id", nargs="?", help="ID of the saved run.")

    failures_parser = subparsers.add_parser(
        "failures",
        help="Show failure details from one saved run.",
        description="Show failure details from one saved run without rerunning it.",
        usage="agent-run failures RUN_ID [--json]",
        add_help=False,
        json_mode=json_mode,
    )
    failures_parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        dest="failures_help",
        help="Show this help message and exit.",
    )
    failures_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Write one structured JSON result to standard output.",
    )
    failures_parser.add_argument("run_id", nargs="?", help="ID of the saved run.")

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

    if parsed.command == "detect" and parsed.detect_help:
        detect_help_text = detect_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": detect_help_text})

        return render_success(json_mode=False, text=detect_help_text)

    if parsed.command == "detect":
        if parsed.add_names is not None and parsed.add_all:
            detect_parser.error("--add and --all cannot be used together")

        if parsed.add_names == [None] and not _is_interactive_terminal():
            detect_parser.error(
                "bare --add requires an interactive terminal; pass --add NAME "
                "or --all instead"
            )

        if parsed.add_names and None in parsed.add_names and parsed.add_names != [None]:
            detect_parser.error(
                "bare --add cannot be combined with a command name; pass names "
                "or use --all"
            )

    if parsed.command == "runs" and parsed.runs_help:
        runs_help_text = runs_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": runs_help_text})

        return render_success(json_mode=False, text=runs_help_text)

    if parsed.command == "prune" and parsed.prune_help:
        prune_help_text = prune_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": prune_help_text})

        return render_success(json_mode=False, text=prune_help_text)

    if parsed.command == "show" and parsed.show_help:
        show_help_text = show_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": show_help_text})

        return render_success(json_mode=False, text=show_help_text)

    if parsed.command == "log" and parsed.log_help:
        log_help_text = log_parser.format_help()

        if parsed.json:
            return render_success(json_mode=True, data={"help": log_help_text})

        return render_success(json_mode=False, text=log_help_text)

    if parsed.command == "failures" and parsed.failures_help:
        failures_help_text = failures_parser.format_help()

        if parsed.json:
            return render_success(
                json_mode=True,
                data={"help": failures_help_text},
            )

        return render_success(json_mode=False, text=failures_help_text)

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
        if parsed.name is not None and separator_index + 1 < len(values):
            run_parser.error("named commands cannot include arguments after --")

        if parsed.name is None and separator_index == len(values):
            run_parser.error("the following arguments are required: name or --")

        direct_arguments = values[separator_index + 1 :]

        if parsed.name is None and not direct_arguments:
            run_parser.error("the following arguments are required: ARGV")

        if parsed.name is not None and parsed.cwd is not None:
            run_parser.error("named commands use their stored working directory")

        if parsed.name is None and (parsed.file or parsed.glob):
            run_parser.error(
                "--file and --glob are only valid for a named command with the "
                "file-list capability"
            )

        connection: sqlite3.Connection | None = None
        log_path: Path | None = None
        run_lock: RunLock | None = None

        try:
            repository = identify_repository()
            database_path = resolve_database_path()
            connection = connect_database(database_path)
            timeout_seconds = parsed.timeout
            resolved_file_paths: tuple[str, ...] = ()
            run_id = uuid4().hex

            if parsed.name is None:
                relative_working_directory = normalise_working_directory(
                    repository, parsed.cwd
                )
                command_arguments = tuple(direct_arguments)

                if starts_browser_runner(command_arguments):
                    return _manual_command_error(
                        json_mode=parsed.json,
                        argv=command_arguments,
                        cwd=repository.root / relative_working_directory,
                    )
            else:
                command = find_command(connection, repository, parsed.name)

                # A manual-only command stops here, before file targets are resolved
                # or a log is opened, so nothing runs and no run record is saved.
                if command.manual:
                    return _manual_command_error(
                        json_mode=parsed.json,
                        argv=command.argv,
                        cwd=repository.root / command.working_directory,
                        name=command.name,
                    )

                run_lock = acquire_run_lock(
                    database_path,
                    repository.id,
                    command.name,
                    run_id=run_id,
                )

                relative_working_directory = normalise_working_directory(
                    repository, command.working_directory
                )
                if timeout_seconds is None:
                    timeout_seconds = command.timeout_seconds

                if parsed.file or parsed.glob:
                    if command.capability != FILE_LIST_CAPABILITY:
                        raise CommandError(
                            f'Command "{command.name}" has capability '
                            f'"{command.capability}"; use agent-run edit '
                            f"{command.name} --capability file-list to accept file targets."
                        )

                    resolved_file_paths = resolve_file_targets(
                        repository,
                        repository.root / relative_working_directory,
                        parsed.file,
                        parsed.glob,
                    )

                command_arguments = command.argv + resolved_file_paths

            if timeout_seconds is None:
                timeout_seconds = DEFAULT_TIMEOUT_SECONDS

            run_id, log_path = create_run_log(database_path, run_id=run_id)

            started_at = datetime.now(timezone.utc).isoformat()
            started_monotonic = time.monotonic()
            interrupted = False

            try:
                result = run_command(
                    command_arguments,
                    repository.root / relative_working_directory,
                    timeout_seconds,
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
                timeout_seconds=timeout_seconds,
                started_at=started_at,
                duration_seconds=result.duration_seconds,
                exit_status=result.exit_status,
                timed_out=result.timed_out,
                log_path=result.log_path,
            )
        except RunBusyError as error:
            return _busy_command_error(
                json_mode=parsed.json,
                name=parsed.name,
                run_id=error.run_id,
            )
        except (RepositoryError, NewerSchemaError, OSError, sqlite3.Error) as error:
            if log_path is not None:
                discard_run_log(log_path)

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
        except (CommandError, ValueError) as error:
            return render_error(
                json_mode=parsed.json,
                code="usage",
                message=str(error),
            )
        finally:
            if run_lock is not None:
                run_lock.release()

            if connection is not None:
                connection.close()

        failure_message = None

        if interrupted:
            failure_message = "Command interrupted."
        elif result.timed_out:
            failure_message = (
                f"Command killed after {_format_timeout(timeout_seconds)}."
            )
        elif result.exit_status != 0:
            failure_message = f"Command exited with status {result.exit_status}."

        if failure_message is None:
            data = _run_record(result, record, resolved_file_paths)
            text = _format_run(result, record)

            if parsed.json:
                return render_success(json_mode=True, data=data)

            return render_success(json_mode=False, text=text)

        failure_message = (
            f"{failure_message} run ID: {record.run_id}; log path: {record.log_path}"
        )
        failure_data = _run_record(result, record, resolved_file_paths)
        # Interrupted runs stop part-way, so their output is not read for failures.
        failure_text = None

        if not interrupted:
            try:
                log_text = record.log_path.read_bytes().decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                failure_report = FailureReport(
                    recognised=False,
                    first=None,
                    more=(),
                    hidden_count=0,
                    truncated=False,
                    tail=(),
                )
            else:
                failure_report = read_failure_report(result.argv, log_text)

            failure_data["failure"] = _failure_report_record(failure_report)
            failure_text = (
                f"Error: {failure_message}\n\n{_format_failure_report(failure_report)}"
            )

        exit_code = render_error(
            json_mode=parsed.json,
            code="check-failed",
            message=failure_message,
            data=failure_data,
            text=failure_text,
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
                    parsed.timeout,
                    parsed.capability,
                    parsed.manual or starts_browser_runner(command_arguments),
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
                    parsed.timeout,
                    parsed.capability,
                    parsed.manual,
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

    if parsed.command == "detect":
        add_names = parsed.add_names
        add_requested = add_names is not None or parsed.add_all

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                registered_commands = {
                    command.name: command
                    for command in list_commands(connection, repository)
                }

                collection = collect_candidates(repository)

                if not add_requested:
                    data = {
                        "candidates": [
                            _candidate_record(candidate, registered_commands)
                            for candidate in collection.candidates
                        ],
                        "skipped": [
                            _candidate_record(candidate, registered_commands)
                            for candidate in collection.skipped
                        ],
                    }
                    text_sections = [
                        _format_candidates(collection.candidates, registered_commands)
                    ]

                    if collection.skipped:
                        text_sections.append(
                            _format_skipped_candidates(collection.skipped)
                        )

                    text = "\n\n".join(text_sections)

                    if parsed.json:
                        return render_success(json_mode=True, data=data)

                    return render_success(json_mode=False, text=text)

                candidate_by_name = {
                    candidate.name: candidate for candidate in collection.candidates
                }

                if parsed.add_all:
                    selected_candidates = collection.candidates
                    reported_candidates = collection.candidates
                elif add_names == [None]:
                    selected_names = _prompt_for_candidates(
                        collection.candidates, registered_commands
                    )
                    selected_candidates = tuple(
                        candidate
                        for candidate in collection.candidates
                        if candidate.name in selected_names
                    )
                    reported_candidates = collection.candidates
                else:
                    requested_names = tuple(add_names or ())
                    missing_names = tuple(
                        name
                        for name in requested_names
                        if name not in candidate_by_name
                    )

                    if missing_names:
                        missing = ", ".join(f'"{name}"' for name in missing_names)
                        detect_parser.error(f"detected command {missing} was not found")

                    selected_candidates = tuple(
                        candidate_by_name[name]
                        for name in dict.fromkeys(requested_names)
                    )
                    reported_candidates = selected_candidates

                added: list[Candidate] = []
                already_registered: list[Candidate] = []
                conflicts: list[Candidate] = []
                names_to_save = {candidate.name for candidate in selected_candidates}

                for candidate in reported_candidates:
                    status = _classify_candidate(candidate, registered_commands)

                    if status == CANDIDATE_SAVED:
                        already_registered.append(candidate)
                    elif status == CANDIDATE_CLASH:
                        conflicts.append(candidate)
                    elif candidate.name in names_to_save:
                        stored_command = add_command(
                            connection,
                            repository,
                            candidate.name,
                            candidate.argv,
                            candidate.working_directory,
                            manual=candidate.manual,
                        )
                        registered_commands[candidate.name] = stored_command
                        added.append(candidate)
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

        data = {
            "added": [
                _candidate_record(candidate, registered_commands) for candidate in added
            ],
            "already_registered": [
                _candidate_record(candidate, registered_commands)
                for candidate in already_registered
            ],
            "conflicts": [
                {
                    **_candidate_record(candidate, registered_commands),
                    "edit_command": _edit_command(candidate),
                }
                for candidate in conflicts
            ],
        }
        text = _format_detect_changes(added, already_registered, conflicts)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "prune":
        try:
            database_path = resolve_database_path()
            connection = connect_database(database_path)
            try:
                records = list_all_runs(connection)
                total_bytes, sizes = measure_log_sizes(
                    resolve_log_directory(database_path), records
                )
                plan = select_prune_candidates(
                    records,
                    now=datetime.now(timezone.utc),
                    sizes=sizes,
                    total_bytes=total_bytes,
                )

                if parsed.apply:
                    removed_candidates = delete_prune_candidates(
                        connection, plan.candidates
                    )
                    remaining_records = list_all_runs(connection)
                    remaining_total_bytes, _ = measure_log_sizes(
                        resolve_log_directory(database_path), remaining_records
                    )
                    plan = RetentionPlan(
                        candidates=removed_candidates,
                        total_bytes=remaining_total_bytes,
                        freed_bytes=sum(
                            candidate.size_bytes for candidate in removed_candidates
                        ),
                    )
            finally:
                connection.close()
        except PruneError as error:
            removed_run_ids = [candidate.record.run_id for candidate in error.removed]
            message = str(error)

            if removed_run_ids:
                message += (
                    f" Runs removed before failure: {', '.join(removed_run_ids)}."
                )

            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=message,
                data={
                    "run_id": error.run_id,
                    "reason": error.reason,
                    "removed_runs": removed_run_ids,
                },
            )
        except (
            NewerSchemaError,
            OSError,
            sqlite3.Error,
            ValueError,
        ) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )

        data = _retention_plan_record(plan, applied=parsed.apply)
        text = _format_retention_plan(plan, applied=parsed.apply)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "runs":
        if parsed.limit <= 0:
            runs_parser.error("--limit must be a positive integer")

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                records = list_runs(connection, repository.id, parsed.limit)
            finally:
                connection.close()
        except (RepositoryError, NewerSchemaError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )

        data = {"runs": [_saved_run_record(record) for record in records]}
        text = _format_runs(records)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "show":
        if parsed.run_id is None or not parsed.run_id.strip():
            show_parser.error(
                "A run ID is required. Run `agent-run runs` to list saved run IDs."
            )

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                record = get_run(connection, parsed.run_id)
            finally:
                connection.close()

            if record.repository_id != repository.id:
                raise RunNotFoundError(f'Run "{parsed.run_id}" was not found.')
        except (RepositoryError, NewerSchemaError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except RunNotFoundError as error:
            return render_error(
                json_mode=parsed.json,
                code="not-found",
                message=str(error),
            )

        data = _saved_run_record(record)
        text = _format_saved_run(record)

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)

    if parsed.command == "log":
        if parsed.run_id is None or not parsed.run_id.strip():
            log_parser.error(
                "A run ID is required. Run `agent-run runs` to list saved run IDs."
            )

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                record = get_run(connection, parsed.run_id)
            finally:
                connection.close()

            if record.repository_id != repository.id:
                raise RunNotFoundError(f'Run "{parsed.run_id}" was not found.')

            log_text = record.log_path.read_bytes().decode("utf-8", errors="replace")

        except (RepositoryError, NewerSchemaError, OSError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except RunNotFoundError as error:
            return render_error(
                json_mode=parsed.json,
                code="not-found",
                message=str(error),
            )

        data = {
            "run_id": record.run_id,
            "log_path": str(record.log_path),
            "log": log_text,
        }

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=log_text)

    if parsed.command == "failures":
        if parsed.run_id is None or not parsed.run_id.strip():
            failures_parser.error(
                "A run ID is required. Run `agent-run runs` to list saved run IDs."
            )

        try:
            repository = identify_repository()
            connection = connect_database()
            try:
                record = get_run(connection, parsed.run_id)
            finally:
                connection.close()

            if record.repository_id != repository.id:
                raise RunNotFoundError(f'Run "{parsed.run_id}" was not found.')
        except (RepositoryError, NewerSchemaError, OSError, sqlite3.Error) as error:
            return render_error(
                json_mode=parsed.json,
                code="environment",
                message=str(error),
            )
        except RunNotFoundError as error:
            return render_error(
                json_mode=parsed.json,
                code="not-found",
                message=str(error),
            )

        if record.exit_status == 0:
            failure_report = FailureReport(
                recognised=False,
                first=None,
                more=(),
                hidden_count=0,
                truncated=False,
                tail=(),
            )
            text = "No failures: the run passed."
        else:
            try:
                log_text = record.log_path.read_bytes().decode(
                    "utf-8", errors="replace"
                )
            except OSError as error:
                return render_error(
                    json_mode=parsed.json,
                    code="environment",
                    message=str(error),
                )

            failure_report = read_failure_report(record.argv, log_text)
            text = _format_failure_report(failure_report)

        data = {
            "run_id": record.run_id,
            "failure": _failure_report_record(failure_report),
        }

        if parsed.json:
            return render_success(json_mode=True, data=data)

        return render_success(json_mode=False, text=text)


def _manual_command_error(
    *,
    json_mode: bool,
    argv: Sequence[str],
    cwd: Path,
    name: str | None = None,
) -> int:
    """Render the refusal for a command that must be started by a person."""
    command_line = f"cd {shlex.quote(str(cwd))} && {shlex.join(argv)}"
    command_label = "This command" if name is None else f'Command "{name}"'

    return render_error(
        json_mode=json_mode,
        code="manual",
        message=(
            f"{command_label} is manual-only. Run it manually with: {command_line}"
        ),
        data={"argv": list(argv), "cwd": str(cwd)},
    )


def _busy_command_error(*, json_mode: bool, name: str, run_id: str) -> int:
    """Render the refusal for a named command that is already running."""
    message = f'Command "{name}" is already running (run ID: {run_id}).'

    return render_error(
        json_mode=json_mode,
        code="busy",
        message=message,
        data={"run_id": run_id},
    )


def _format_command(command: Command) -> str:
    """Format one named command for human-readable output."""
    timeout = (
        "default"
        if command.timeout_seconds is None
        else _format_timeout(command.timeout_seconds)
    )

    return render_row_group(
        [
            {"label": "name", "value": command.name},
            {"label": "working directory", "value": command.working_directory},
            {"label": "capability", "value": command.capability},
            {"label": "manual", "value": str(command.manual).lower()},
            {"label": "timeout", "value": timeout},
            {"label": "argv", "value": json.dumps(list(command.argv))},
        ]
    )


def _format_timeout(timeout_seconds: float) -> str:
    """Format a timeout value with the unit shown to a human."""
    unit = "second" if timeout_seconds == 1 else "seconds"
    return f"{timeout_seconds:g} {unit}"


def _format_commands(commands: Sequence[Command]) -> str:
    """Format named commands as separated human-readable records."""
    if not commands:
        return render_empty_state(title="No named commands registered")

    return "\n\n".join(_format_command(command) for command in commands)


def _is_interactive_terminal() -> bool:
    """Return whether someone at a terminal can answer the checkbox list."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _classify_candidate(
    candidate: Candidate, registered_commands: Mapping[str, Command]
) -> str:
    """Return whether a detected command is new, saved exactly, or clashes.

    A clash is a saved command with the same name but a different command or
    working directory; it is never replaced automatically.
    """
    registered_command = registered_commands.get(candidate.name)

    if registered_command is None:
        return CANDIDATE_NEW

    if (
        registered_command.argv == candidate.argv
        and registered_command.working_directory == candidate.working_directory
    ):
        return CANDIDATE_SAVED

    return CANDIDATE_CLASH


def _edit_command(candidate: Candidate) -> str:
    """Return the `agent-run edit` command that replaces the saved command with the detected one."""
    return shlex.join(
        [
            "agent-run",
            "edit",
            candidate.name,
            "--cwd",
            candidate.working_directory,
            "--",
            *candidate.argv,
        ]
    )


def _prompt_for_candidates(
    candidates: Sequence[Candidate], registered_commands: Mapping[str, Command]
) -> tuple[str, ...]:
    """Show a checkbox list of detected commands and return the ticked names.

    New commands start ticked. Saved and clashing commands are listed but
    cannot be ticked. Cancelling the list returns no names.
    """
    if not candidates:
        return ()

    choices = []

    for candidate in candidates:
        status = _classify_candidate(candidate, registered_commands)
        disabled = None
        checked = status == CANDIDATE_NEW

        if status == CANDIDATE_SAVED:
            disabled = "already registered"
        elif status == CANDIDATE_CLASH:
            disabled = f"conflict; use {_edit_command(candidate)}"

        choices.append(
            questionary.Choice(
                title=(
                    f"{candidate.name}: {json.dumps(list(candidate.argv))} "
                    f"(cwd: {candidate.working_directory})"
                ),
                value=candidate.name,
                disabled=disabled,
                checked=checked,
            )
        )

    selected_names = questionary.checkbox(
        "Select detected commands to add:", choices=choices
    ).ask()

    return tuple(selected_names or ())


def _candidate_record(
    candidate: Candidate, registered_commands: Mapping[str, Command]
) -> dict[str, object]:
    """Return one detected candidate with its registration status."""
    return {
        "name": candidate.name,
        "working_directory": candidate.working_directory,
        "argv": list(candidate.argv),
        "detector": candidate.detector,
        "registered": _classify_candidate(candidate, registered_commands)
        == CANDIDATE_SAVED,
    }


def _format_candidate(candidate: Candidate, registered: bool) -> str:
    """Format one detected candidate for human-readable output."""
    return "\n".join(
        [
            f"name: {candidate.name}",
            f"working directory: {candidate.working_directory}",
            f"argv: {json.dumps(list(candidate.argv))}",
            f"detector: {candidate.detector}",
            f"registered: {str(registered).lower()}",
        ]
    )


def _format_candidates(
    candidates: Sequence[Candidate], registered_commands: Mapping[str, Command]
) -> str:
    """Format detected candidates, or explain that none were found."""
    if not candidates:
        return "No commands detected."

    return "\n\n".join(
        _format_candidate(
            candidate,
            _classify_candidate(candidate, registered_commands) == CANDIDATE_SAVED,
        )
        for candidate in candidates
    )


def _format_skipped_candidates(skipped: Sequence[Candidate]) -> str:
    """List candidates dropped because an earlier detector already used their name."""
    lines = ["Skipped duplicate candidates:"]
    lines.extend(
        "detector: "
        f"{candidate.detector}; name: {candidate.name}; "
        f"argv: {json.dumps(list(candidate.argv))}"
        for candidate in skipped
    )

    return "\n".join(lines)


def _format_detect_changes(
    added: Sequence[Candidate],
    already_registered: Sequence[Candidate],
    conflicts: Sequence[Candidate],
) -> str:
    """Format what `detect --add` or `--all` saved, found already saved, or skipped because of a clash."""
    sections = []

    if added:
        sections.append(
            "Added detected commands:\n"
            + "\n".join(f"- {candidate.name}" for candidate in added)
        )

    if already_registered:
        sections.append(
            "Already registered:\n"
            + "\n".join(f"- {candidate.name}" for candidate in already_registered)
        )

    if conflicts:
        sections.append(
            "Conflicting registered commands:\n"
            + "\n".join(
                f"- {candidate.name}: edit with {_edit_command(candidate)}"
                for candidate in conflicts
            )
        )

    if not sections:
        return "No detected commands were added."

    return "\n\n".join(sections)


def _saved_run_record(record: RunRecord) -> dict[str, object]:
    """Return the public fields for a stored run record."""
    return {
        "run_id": record.run_id,
        "argv": list(record.argv),
        "working_directory": record.working_directory,
        "timeout_seconds": record.timeout_seconds,
        "started_at": record.started_at,
        "duration_seconds": record.duration_seconds,
        "exit_status": record.exit_status,
        "timed_out": record.timed_out,
        "log_path": str(record.log_path),
    }


def _format_saved_run(record: RunRecord) -> str:
    """Format one stored run record for human-readable output."""
    rows = render_row_group(
        [
            {"label": "run ID", "value": record.run_id},
            {"label": "command", "value": json.dumps(list(record.argv))},
            {"label": "working directory", "value": record.working_directory},
            {"label": "timeout", "value": _format_timeout(record.timeout_seconds)},
            {"label": "started at", "value": record.started_at},
            {"label": "exit status", "value": record.exit_status},
            {"label": "timed out", "value": str(record.timed_out).lower()},
            {"label": "duration", "value": f"{record.duration_seconds:.3f}s"},
        ]
    )

    return f"{rows}\nlog path: {record.log_path}"


def _format_runs(records: Sequence[RunRecord]) -> str:
    """Format saved runs for text output, one block per run, or say there are none."""
    if not records:
        return render_empty_state(title="No runs recorded")

    return "\n\n".join(_format_saved_run(record) for record in records)


def _retention_plan_record(
    plan: RetentionPlan,
    *,
    applied: bool = False,
) -> dict[str, object]:
    """Return the public fields for a retention preview or applied deletion."""
    return {
        "applied": applied,
        "runs": [
            {
                "run_id": candidate.record.run_id,
                "reason": candidate.reason,
                "space_freed_bytes": candidate.size_bytes,
            }
            for candidate in plan.candidates
        ],
        "total_log_bytes": plan.total_bytes,
        "space_freed_bytes": plan.freed_bytes,
    }


def _format_retention_plan(plan: RetentionPlan, *, applied: bool = False) -> str:
    """Format a retention preview or applied deletion for human-readable output."""
    lines = [
        f"total log size: {plan.total_bytes} bytes",
        f"space freed: {plan.freed_bytes} bytes",
    ]

    if not plan.candidates:
        lines.append("No runs were removed." if applied else "No runs need pruning.")
    else:
        lines.append("Runs removed:" if applied else "Runs to remove:")
        lines.extend(
            "run ID: "
            f"{candidate.record.run_id}; reason: {candidate.reason}; "
            f"space freed: {candidate.size_bytes} bytes"
            for candidate in plan.candidates
        )

    return "\n".join(lines)


def _command_record(command: Command) -> dict[str, object]:
    """Return the public fields for one command result."""
    return {
        "name": command.name,
        "working_directory": command.working_directory,
        "capability": command.capability,
        "manual": command.manual,
        "timeout_seconds": command.timeout_seconds,
        "argv": list(command.argv),
    }


def _run_record(
    result: RunResult, record: RunRecord, files: Sequence[str]
) -> dict[str, object]:
    """Return the public fields for one named or direct command result.

    Args:
        result: Outcome of the command process.
        record: Saved run record holding the run ID and log path.
        files: Paths resolved from `--file` and `--glob`, empty for a direct run.
    """
    return {
        "argv": list(result.argv),
        "files": list(files),
        "working_directory": str(result.working_directory),
        "exit_status": result.exit_status,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
        "run_id": record.run_id,
        "log_path": str(record.log_path),
    }


def _failure_record(failure: Failure) -> dict[str, object]:
    """Return the public fields for one extracted failure."""
    return {
        "path": failure.path,
        "line": failure.line,
        "column": failure.column,
        "title": failure.title,
        "detail": list(failure.detail),
    }


def _failure_report_record(report: FailureReport) -> dict[str, object]:
    """Return the failure report as the fields placed in `error.data.failure`."""
    return {
        "recognised": report.recognised,
        "first": (None if report.first is None else _failure_record(report.first)),
        "more": [_failure_record(failure) for failure in report.more],
        "hidden_count": report.hidden_count,
        "truncated": report.truncated,
        "tail": list(report.tail),
    }


def _format_failure_report(report: FailureReport) -> str:
    """Format the same bounded failure evidence shown in JSON data."""
    if not report.recognised or report.first is None:
        if not report.tail:
            return "Failure output was not recognised."

        tail_count = len(report.tail)
        tail_unit = "line" if tail_count == 1 else "lines"

        return "\n".join(
            [f"Failure output (last {tail_count} {tail_unit}):", *report.tail]
        )

    lines = ["First failure:", _format_failure_line(report.first)]
    lines.extend(
        line
        for line in report.first.detail
        if not _is_repeated_failure_location(line, report.first)
    )

    if report.more:
        lines.extend(["Additional failures:"])
        lines.extend(_format_failure_line(failure) for failure in report.more)

    if report.hidden_count:
        lines.append(
            f"{report.hidden_count} additional failure(s) hidden by the limit."
        )

    if report.truncated:
        lines.append("Some failure details were truncated by the limit.")

    return "\n".join(lines)


def _is_repeated_failure_location(line: str, failure: Failure) -> bool:
    """Return whether a detail line repeats the failure's source location."""
    if not failure.path or failure.line is None:
        return False

    location = f"{failure.path}:{failure.line}"

    if failure.column is not None:
        location = f"{location}:{failure.column}"

    return line.startswith(f"{location}:")


def _format_failure_line(failure: Failure) -> str:
    """Format one failure as a compact source location and title line."""
    location_parts = [failure.path] if failure.path else []

    if failure.line is not None:
        location_parts.append(str(failure.line))

        if failure.column is not None:
            location_parts.append(str(failure.column))

    location = ":".join(location_parts)

    return f"{location}: {failure.title}" if location else failure.title


def _format_run(result: RunResult, record: RunRecord) -> str:
    """Format one human-readable result block for a completed command."""
    result_text = render_command_result(
        result="success",
        summary="Command completed",
        command=shlex.join(result.argv),
        exit_code=result.exit_status,
        duration=f"{result.duration_seconds:.3f}s",
        detail=f"Run ID: {record.run_id}",
    )

    return f"{result_text}\nlog path: {record.log_path}"


if __name__ == "__main__":
    raise SystemExit(main())
