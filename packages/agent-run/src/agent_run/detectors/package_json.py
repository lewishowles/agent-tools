"""Detect commands from scripts in the repository's root package.json."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_run.commands import starts_browser_runner
from agent_run.repository import Repository

if TYPE_CHECKING:
    from agent_run.detectors import Candidate

# The command that runs a script for each package manager, keyed by its lockfile. The
# first lockfile found at the repository root wins, and npm is used when none exists.
_PACKAGE_RUNNERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("package-lock.json", ("npm", "run")),
    ("pnpm-lock.yaml", ("pnpm", "run")),
    ("yarn.lock", ("yarn",)),
    ("bun.lock", ("bun", "run")),
    ("bun.lockb", ("bun", "run")),
)

# Shell operators that end one step of a package script and start the next.
_STEP_SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})


def detect_package_json_scripts(repository: Repository) -> tuple[Candidate, ...]:
    """Return one root-level candidate for every package.json script.

    Args:
        repository: Repository whose root package.json should be inspected.

    Returns:
        Candidates that run each script through the detected package manager, or an
        empty tuple when the root package.json has no usable scripts.
    """
    package = _read_package_json(repository.root / "package.json")
    scripts = package.get("scripts")

    if not isinstance(scripts, Mapping):
        return ()

    runner = _package_runner(repository.root)

    # Imported here because the detectors package imports this module to build its list.
    from agent_run.detectors import Candidate

    return tuple(
        Candidate(
            name=name,
            argv=(*runner, name),
            working_directory=".",
            detector="package.json",
            manual=_script_is_manual(script),
        )
        for name, script in scripts.items()
    )


def _read_package_json(path: Path) -> dict[str, Any]:
    """Read a package manifest, treating a missing or malformed file as empty."""
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}

    if not isinstance(package, dict):
        return {}

    return package


def _script_is_manual(script: object) -> bool:
    """Return whether a package script starts Playwright or Cypress in any of its chained steps.

    Only the script's own text is read, so a script that calls another script is not followed.
    """
    if not isinstance(script, str):
        return False

    try:
        tokens = shlex.split(script)
    except ValueError:
        return False

    step_starts = [0] + [
        index + 1 for index, token in enumerate(tokens) if token in _STEP_SEPARATORS
    ]

    return any(starts_browser_runner(tokens[start:]) for start in step_starts)


def _package_runner(root: Path) -> tuple[str, ...]:
    """Return the command that runs a script, chosen by the lockfile at the root."""
    for lockfile, runner in _PACKAGE_RUNNERS:
        if (root / lockfile).exists():
            return runner

    return ("npm", "run")
