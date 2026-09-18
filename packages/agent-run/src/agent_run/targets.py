"""Resolve file targets for commands that accept a file list."""

import os
from collections.abc import Sequence
from pathlib import Path

from agent_run.repository import Repository


class TargetError(ValueError):
    """Report a file target that cannot be passed to a command."""


def resolve_file_targets(
    repository: Repository,
    working_directory: Path,
    requested_paths: Sequence[str],
) -> tuple[str, ...]:
    """Resolve file targets and return unique paths relative to the run directory.

    Args:
        repository: Local repository that owns the run.
        working_directory: Absolute directory used to run the command.
        requested_paths: File paths resolved from the current directory.

    Raises:
        TargetError: If a path is missing, not a regular file, or outside the
            repository.
    """
    repository_root = repository.root.resolve()
    run_directory = working_directory.resolve()
    current_directory = Path.cwd()
    resolved_paths: list[str] = []
    seen_paths: set[Path] = set()

    for requested_path in requested_paths:
        path = Path(requested_path).expanduser()

        if not path.is_absolute():
            path = current_directory / path

        resolved_path = path.resolve()

        try:
            resolved_path.relative_to(repository_root)
        except ValueError as error:
            raise TargetError(
                f"File target must be inside the repository: {requested_path}"
            ) from error

        if not resolved_path.exists():
            raise TargetError(f"File target does not exist: {requested_path}")

        if not resolved_path.is_file():
            raise TargetError(f"File target is not a regular file: {requested_path}")

        if resolved_path in seen_paths:
            continue

        seen_paths.add(resolved_path)
        resolved_paths.append(os.path.relpath(resolved_path, run_directory))

    return tuple(resolved_paths)
