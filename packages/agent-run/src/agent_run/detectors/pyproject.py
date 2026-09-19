"""Detect Python checks configured in the repository's root pyproject.toml."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomllib

from agent_run.repository import Repository

if TYPE_CHECKING:
    from agent_run.detectors import Candidate


def detect_pyproject_checks(repository: Repository) -> tuple[Candidate, ...]:
    """Return candidates for the Python checks configured at the repository root.

    Args:
        repository: Repository whose root pyproject.toml should be inspected.

    Returns:
        Candidates for pytest and Ruff checks, or an empty tuple when the file is
        missing, malformed, or does not configure either check.
    """
    pyproject = _read_pyproject(repository.root / "pyproject.toml")
    tool_config = pyproject.get("tool")

    if not isinstance(tool_config, Mapping):
        return ()

    runner = ("uv", "run") if (repository.root / "uv.lock").exists() else ()

    # Imported here because the detectors package imports this module to build its list.
    from agent_run.detectors import Candidate

    candidates: list[Candidate] = []
    pytest_config = tool_config.get("pytest")
    if isinstance(pytest_config, Mapping) and isinstance(
        pytest_config.get("ini_options"), Mapping
    ):
        candidates.append(
            Candidate(
                name="pytest",
                argv=(*runner, "pytest"),
                working_directory=".",
                detector="pyproject.toml",
            )
        )

    if isinstance(tool_config.get("ruff"), Mapping):
        candidates.append(
            Candidate(
                name="ruff",
                argv=(*runner, "ruff", "check"),
                working_directory=".",
                detector="pyproject.toml",
            )
        )

    return tuple(candidates)


def _read_pyproject(path: Path) -> dict[str, Any]:
    """Read a pyproject file, treating a missing or malformed file as empty."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
