"""Store and retrieve named commands for a local repository."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_run.repository import Repository


class CommandError(RuntimeError):
    """Report a command that cannot be added to the local repository."""


@dataclass(frozen=True)
class Command:
    """Describe a stored command and the directory where it runs.

    Attributes:
        name: Name used to select the command.
        argv: Argument array passed to the command process.
        working_directory: Repository-relative directory, using `.` for the root.
        created_at: UTC timestamp recorded when the command was added.
    """

    name: str
    argv: tuple[str, ...]
    working_directory: str
    created_at: str


def _normalise_working_directory(
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


def _command_from_row(row: sqlite3.Row | tuple[object, ...]) -> Command:
    """Decode one database row into a stored command."""
    name, argv, working_directory, created_at = row
    return Command(
        name=str(name),
        argv=tuple(json.loads(str(argv))),
        working_directory=str(working_directory),
        created_at=str(created_at),
    )


def add_command(
    connection: sqlite3.Connection,
    repository: Repository,
    name: str,
    argv: Sequence[str],
    cwd: str | Path | None = None,
) -> Command:
    """Add a named command for a repository and return the stored command.

    Args:
        connection: Open agent-run database connection.
        repository: Local repository that owns the command.
        name: Name used to select the command later.
        argv: Non-empty argument array passed to the command process.
        cwd: Optional directory relative to the repository root.

    Raises:
        CommandError: If the arguments, directory, or name are invalid.
    """
    command_arguments = _validate_name_and_arguments(name, argv)
    relative_working_directory = _normalise_working_directory(repository, cwd)

    created_at = datetime.now(timezone.utc).isoformat()

    try:
        connection.execute(
            """
            INSERT INTO commands (
                repository_id, name, argv, working_directory, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                repository.id,
                name,
                json.dumps(command_arguments),
                relative_working_directory,
                created_at,
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
        SELECT name, argv, working_directory, created_at
        FROM commands
        WHERE repository_id = ?
        ORDER BY name
        """,
        (repository.id,),
    ).fetchall()

    return [_command_from_row(row) for row in rows]
