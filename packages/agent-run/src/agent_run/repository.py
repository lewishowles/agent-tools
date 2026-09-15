"""Identify the Git repository that contains the current path."""

import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Git config key that stores the identity shared by a clone and its worktrees.
_REPOSITORY_ID_KEY = "agent-run.repository-id"


class RepositoryError(RuntimeError):
    """Report that the local repository could not be identified or updated."""


@dataclass(frozen=True)
class Repository:
    """Describe the local Git repository used by agent-run.

    Attributes:
        root: Absolute path to the repository's top-level directory.
        id: Stable identifier stored in the clone-local Git configuration.
    """

    root: Path
    id: str


def _run_git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a Git command in ``root`` and raise RepositoryError when Git cannot start."""
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        if error.filename == "git":
            raise RepositoryError("Git is not available on PATH.") from error

        raise RepositoryError("Git could not be started.") from error


def find_repository_root(start: str | Path | None = None) -> Path:
    """Return the absolute Git root containing ``start``.

    Raises:
        RepositoryError: If ``start`` is not a directory, Git cannot start,
            ``start`` is outside a repository, or Git returns no root.
    """
    start_path = Path.cwd() if start is None else Path(start)
    start_path = start_path.expanduser().resolve()

    if not start_path.is_dir():
        raise RepositoryError(f"{start_path} is not a directory.")

    result = _run_git(start_path, ["rev-parse", "--show-toplevel"])

    if result.returncode != 0:
        raise RepositoryError(f"{start_path} is not inside a Git repository.")

    repository_root = result.stdout.strip()
    if not repository_root:
        raise RepositoryError("Git did not return a repository root.")

    return Path(repository_root).resolve()


def ensure_repository_id(root: str | Path) -> str:
    """Return the clone-local repository ID, creating it when it is absent.

    The ID lives in Git's local configuration, so it is not committed or copied
    to a separately cloned repository. Git worktrees use the same local config.

    Raises:
        RepositoryError: If Git cannot read or write the local configuration.
    """
    repository_root = Path(root).expanduser().resolve()
    result = _run_git(
        repository_root,
        ["config", "--local", "--get", _REPOSITORY_ID_KEY],
    )

    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    if result.returncode not in (0, 1):
        raise RepositoryError("Git could not read the local repository ID.")

    repository_id = str(uuid.uuid4())
    result = _run_git(
        repository_root,
        ["config", "--local", _REPOSITORY_ID_KEY, repository_id],
    )
    if result.returncode != 0:
        raise RepositoryError("Git could not write the local repository ID.")

    return repository_id


def identify_repository(start: str | Path | None = None) -> Repository:
    """Return the root and stable ID for the repository containing ``start``."""
    root = find_repository_root(start)
    repository_id = ensure_repository_id(root)

    return Repository(root=root, id=repository_id)
