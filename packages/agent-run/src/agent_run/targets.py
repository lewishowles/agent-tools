"""Resolve file targets for commands that accept a file list."""

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from agent_run.repository import Repository


class TargetError(ValueError):
    """Report a file target that cannot be passed to a command."""


def resolve_file_targets(
    repository: Repository,
    working_directory: Path,
    requested_paths: Sequence[str],
    requested_globs: Sequence[str],
) -> tuple[str, ...]:
    """Resolve file targets and return unique paths relative to the run directory.

    Args:
        repository: Local repository that owns the run.
        working_directory: Absolute directory used to run the command.
        requested_paths: File paths resolved from the current directory.
        requested_globs: Git glob patterns resolved from the repository root.

    Raises:
        TargetError: If a path is missing, not a regular file, outside the
            repository, or a glob matches no existing, non-ignored files.
    """
    repository_root = repository.root.resolve()
    run_directory = working_directory.resolve()
    current_directory = Path.cwd()
    resolved_paths: list[str] = []
    seen_paths: set[Path] = set()

    for requested_path in requested_paths:
        resolved_path = _resolve_target(
            repository_root, current_directory, requested_path
        )

        if resolved_path in seen_paths:
            continue

        seen_paths.add(resolved_path)
        resolved_paths.append(os.path.relpath(resolved_path, run_directory))

    for requested_glob in requested_globs:
        matched_paths = _expand_file_glob(repository_root, requested_glob)

        for matched_path in matched_paths:
            resolved_path = _resolve_target(
                repository_root, repository_root, matched_path
            )

            if resolved_path in seen_paths:
                continue

            seen_paths.add(resolved_path)
            resolved_paths.append(os.path.relpath(resolved_path, run_directory))

    return tuple(resolved_paths)


def _resolve_target(
    repository_root: Path,
    base_directory: Path,
    requested_path: str,
) -> Path:
    """Resolve one target and return its validated absolute path.

    Args:
        repository_root: Resolved repository root that every target must sit under.
        base_directory: Directory a relative target is resolved from.
        requested_path: Target as the user or Git gave it, used in error messages.

    Raises:
        TargetError: If the target is outside the repository, missing, or not a
            regular file.
    """
    path = Path(requested_path).expanduser()

    if not path.is_absolute():
        path = base_directory / path

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

    return resolved_path


def _expand_file_glob(repository_root: Path, requested_glob: str) -> tuple[str, ...]:
    """Return the sorted repository-relative files that match one glob.

    Git does the matching, so files covered by `.gitignore` are left out. Files
    Git still tracks but that are gone from disk are also left out, since the
    user never named them.

    Args:
        repository_root: Directory the pattern is matched from.
        requested_glob: Pattern in Git glob syntax, where `**` matches any depth.

    Raises:
        TargetError: If Git cannot run or the pattern matches nothing.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                f":(glob){requested_glob}",
            ],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise TargetError("Git is not available to expand file globs.") from error

    if result.returncode != 0:
        raise TargetError(f"Could not expand glob pattern: {requested_glob}")

    matched_paths = tuple(
        sorted(
            path
            for path in result.stdout.split("\0")
            if path and (repository_root / path).is_file()
        )
    )

    if not matched_paths:
        raise TargetError(
            f"Glob pattern matches no existing, non-ignored files: {requested_glob}"
        )

    return matched_paths
