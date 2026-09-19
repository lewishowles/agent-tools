"""Store and retrieve named commands for a local repository."""

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_run.repository import Repository

# Capability of a command that takes no run inputs.
DEFAULT_CAPABILITY = "none"
# Capability of a command that accepts file paths appended with `--file`.
FILE_LIST_CAPABILITY = "file-list"
# Every capability a stored command may hold, offered as argparse choices.
COMMAND_CAPABILITIES: tuple[str, ...] = (DEFAULT_CAPABILITY, FILE_LIST_CAPABILITY)

# Browser test runners that a person must start; agents never run them.
_BROWSER_RUNNERS: tuple[str, ...] = ("playwright", "cypress")
# Package-runner prefixes that can come before a browser test runner.
_BROWSER_RUNNER_WRAPPERS: tuple[tuple[str, ...], ...] = (
    ("npx",),
    ("pnpm", "exec"),
    ("yarn",),
    ("bunx",),
    ("uv", "run"),
)


class CommandError(RuntimeError):
    """Report a command that cannot be stored or changed."""


class CommandNotFoundError(CommandError):
    """Report a named command that does not exist for the repository."""


def starts_browser_runner(argv: Sequence[str]) -> bool:
    """Return whether an argument array starts Playwright or Cypress, directly or through a package runner."""
    command_arguments = tuple(argv)

    if command_arguments and command_arguments[0] in _BROWSER_RUNNERS:
        return True

    for wrapper in _BROWSER_RUNNER_WRAPPERS:
        if command_arguments[: len(wrapper)] != wrapper:
            continue

        runner_index = len(wrapper)
        return (
            runner_index < len(command_arguments)
            and command_arguments[runner_index] in _BROWSER_RUNNERS
        )

    return False


@dataclass(frozen=True)
class Command:
    """Describe a stored command and the directory where it runs.

    Attributes:
        name: Name used to select the command.
        argv: Argument array passed to the command process.
        working_directory: Repository-relative directory, using `.` for the root.
        created_at: UTC timestamp recorded when the command was added.
        timeout_seconds: Optional timeout override, or `None` to use the default.
        capability: Inputs that a named run may append to the command.
        manual: Whether the command must be run manually instead of automatically.
    """

    name: str
    argv: tuple[str, ...]
    working_directory: str
    created_at: str
    timeout_seconds: float | None = None
    capability: str = DEFAULT_CAPABILITY
    manual: bool = False


def normalise_working_directory(
    repository: Repository, working_directory: str | Path | None
) -> str:
    """Validate a working directory and return its repository-relative path."""
    repository_root = repository.root.resolve()
    requested_path = (
        repository_root
        if working_directory is None
        else Path(working_directory).expanduser()
    )

    if not requested_path.is_absolute():
        requested_path = repository_root / requested_path

    resolved_path = requested_path.resolve()

    try:
        relative_path = resolved_path.relative_to(repository_root)
    except ValueError as error:
        raise CommandError(
            f"Working directory must be inside the repository: {requested_path}"
        ) from error

    if not resolved_path.exists():
        raise CommandError(f"Working directory does not exist: {requested_path}")

    if not resolved_path.is_dir():
        raise CommandError(f"Working directory is not a directory: {requested_path}")

    return str(relative_path)


def _validate_name_and_arguments(name: str, argv: Sequence[str]) -> tuple[str, ...]:
    """Validate a command name and argument array, returning a tuple."""
    if not name.strip():
        raise CommandError("Command name must not be empty.")

    command_arguments = tuple(argv)

    if not command_arguments:
        raise CommandError("Command arguments must contain at least one argument.")

    return command_arguments


def _validate_timeout(timeout_seconds: float | None) -> float | None:
    """Validate an optional command timeout and return it unchanged."""
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise CommandError("Command timeout must be finite and greater than zero.")

    return timeout_seconds


def _validate_capability(capability: str) -> str:
    """Validate a command capability and return it unchanged."""
    if capability not in COMMAND_CAPABILITIES:
        capabilities = ", ".join(COMMAND_CAPABILITIES)
        raise CommandError(f"Command capability must be one of: {capabilities}.")

    return capability


def _command_from_row(row: sqlite3.Row | tuple[object, ...]) -> Command:
    """Decode one database row into a stored command."""
    (
        name,
        argv,
        working_directory,
        created_at,
        timeout_seconds,
        capability,
        manual,
    ) = row
    return Command(
        name=str(name),
        argv=tuple(json.loads(str(argv))),
        working_directory=str(working_directory),
        created_at=str(created_at),
        timeout_seconds=(None if timeout_seconds is None else float(timeout_seconds)),
        capability=str(capability),
        manual=bool(manual),
    )


def find_command(
    connection: sqlite3.Connection, repository: Repository, name: str
) -> Command:
    """Return a repository command by name or raise a not-found error.

    Args:
        connection: Open agent-run database connection.
        repository: Local repository whose command should be returned.
        name: Name of the command to find.

    Raises:
        CommandNotFoundError: If the command is not registered for the repository.
    """
    row = connection.execute(
        """
        SELECT name, argv, working_directory, created_at, timeout_seconds, capability,
               manual
        FROM commands
        WHERE repository_id = ? AND name = ?
        """,
        (repository.id, name),
    ).fetchone()

    if row is None:
        raise CommandNotFoundError(f'Command "{name}" was not found.')

    return _command_from_row(row)


def add_command(
    connection: sqlite3.Connection,
    repository: Repository,
    name: str,
    argv: Sequence[str],
    cwd: str | Path | None = None,
    timeout_seconds: float | None = None,
    capability: str = DEFAULT_CAPABILITY,
    manual: bool = False,
) -> Command:
    """Add a named command for a repository and return the stored command.

    Args:
        connection: Open agent-run database connection.
        repository: Local repository that owns the command.
        name: Name used to select the command later.
        argv: Non-empty argument array passed to the command process.
        cwd: Optional directory relative to the repository root.
        timeout_seconds: Optional positive timeout override for the command.
        capability: Inputs that a named run may append to the command.
        manual: Whether the command must be run manually instead of automatically.

    Raises:
        CommandError: If the arguments, directory, or name are invalid.
    """
    command_arguments = _validate_name_and_arguments(name, argv)

    timeout_seconds = _validate_timeout(timeout_seconds)
    capability = _validate_capability(capability)

    relative_working_directory = normalise_working_directory(repository, cwd)

    created_at = datetime.now(timezone.utc).isoformat()

    try:
        connection.execute(
            """
            INSERT INTO commands (
                repository_id,
                name,
                argv,
                working_directory,
                created_at,
                timeout_seconds,
                capability,
                manual
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repository.id,
                name,
                json.dumps(command_arguments),
                relative_working_directory,
                created_at,
                timeout_seconds,
                capability,
                manual,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise CommandError(
            f'Command "{name}" already exists. Use agent-run edit {name} to change it.'
        ) from error

    return Command(
        name=name,
        argv=command_arguments,
        working_directory=relative_working_directory,
        created_at=created_at,
        timeout_seconds=timeout_seconds,
        capability=capability,
        manual=manual,
    )


def list_commands(
    connection: sqlite3.Connection, repository: Repository
) -> list[Command]:
    """Return a repository's named commands ordered by name.

    Args:
        connection: Open agent-run database connection.
        repository: Local repository whose commands should be returned.
    """
    rows = connection.execute(
        """
        SELECT name, argv, working_directory, created_at, timeout_seconds, capability,
               manual
        FROM commands
        WHERE repository_id = ?
        ORDER BY name
        """,
        (repository.id,),
    ).fetchall()

    return [_command_from_row(row) for row in rows]


def edit_command(
    connection: sqlite3.Connection,
    repository: Repository,
    name: str,
    argv: Sequence[str] | None = None,
    cwd: str | Path | None = None,
    timeout_seconds: float | None = None,
    capability: str | None = None,
    manual: bool | None = None,
) -> Command:
    """Change the stored settings of a named command.

    Args:
        connection: Open agent-run database connection.
        repository: Local repository that owns the command.
        name: Name of the command to change.
        argv: Optional non-empty replacement argument array.
        cwd: Optional replacement directory relative to the repository root.
        timeout_seconds: Optional positive replacement timeout override.
        capability: Optional replacement for the command's run input capability.
        manual: Optional replacement for the command's manual-only mark. When it is
            left out and the new arguments start Playwright or Cypress, the
            command is marked manual-only.

    Raises:
        CommandNotFoundError: If the command is not registered for the repository.
        CommandError: If no field is supplied, or a replacement is invalid.
    """
    command = find_command(connection, repository, name)

    timeout_seconds = _validate_timeout(timeout_seconds)

    if (
        argv is None
        and cwd is None
        and timeout_seconds is None
        and capability is None
        and manual is None
    ):
        raise CommandError(
            "Edit must change arguments, working directory, timeout, capability, or "
            "manual mark."
        )

    command_arguments = (
        command.argv if argv is None else _validate_name_and_arguments(name, argv)
    )
    relative_working_directory = (
        command.working_directory
        if cwd is None
        else normalise_working_directory(repository, cwd)
    )
    command_timeout_seconds = command.timeout_seconds

    if timeout_seconds is not None:
        command_timeout_seconds = timeout_seconds

    command_capability = (
        command.capability if capability is None else _validate_capability(capability)
    )
    command_manual = command.manual if manual is None else manual

    # New arguments that start a browser runner make the command manual-only, unless
    # this edit sets the mark itself.
    if manual is None and argv is not None and starts_browser_runner(command_arguments):
        command_manual = True

    connection.execute(
        """
        UPDATE commands
        SET argv = ?, working_directory = ?, timeout_seconds = ?, capability = ?, manual = ?
        WHERE repository_id = ? AND name = ?
        """,
        (
            json.dumps(command_arguments),
            relative_working_directory,
            command_timeout_seconds,
            command_capability,
            command_manual,
            repository.id,
            name,
        ),
    )

    return Command(
        name=command.name,
        argv=command_arguments,
        working_directory=relative_working_directory,
        created_at=command.created_at,
        timeout_seconds=command_timeout_seconds,
        capability=command_capability,
        manual=command_manual,
    )


def rename_command(
    connection: sqlite3.Connection,
    repository: Repository,
    name: str,
    new_name: str,
) -> Command:
    """Rename a named command and return its updated record.

    Args:
        connection: Open agent-run database connection.
        repository: Local repository that owns the command.
        name: Current name of the command.
        new_name: Replacement name for the command.

    Raises:
        CommandNotFoundError: If the current name is not registered for the repository.
        CommandError: If the replacement name is empty or already registered.
    """
    if not new_name.strip():
        raise CommandError("Command name must not be empty.")

    command = find_command(connection, repository, name)

    try:
        connection.execute(
            """
            UPDATE commands
            SET name = ?
            WHERE repository_id = ? AND name = ?
            """,
            (new_name, repository.id, name),
        )
    except sqlite3.IntegrityError as error:
        raise CommandError(
            f'Command "{new_name}" already exists. Use agent-run edit {new_name} to change it.'
        ) from error

    return Command(
        name=new_name,
        argv=command.argv,
        working_directory=command.working_directory,
        created_at=command.created_at,
        timeout_seconds=command.timeout_seconds,
        capability=command.capability,
        manual=command.manual,
    )


def remove_command(
    connection: sqlite3.Connection, repository: Repository, name: str
) -> None:
    """Remove a named command from a repository.

    Args:
        connection: Open agent-run database connection.
        repository: Local repository that owns the command.
        name: Name of the command to remove.

    Raises:
        CommandNotFoundError: If the command is not registered for the repository.
    """
    cursor = connection.execute(
        """
        DELETE FROM commands
        WHERE repository_id = ? AND name = ?
        """,
        (repository.id, name),
    )

    if cursor.rowcount == 0:
        raise CommandNotFoundError(f'Command "{name}" was not found.')
