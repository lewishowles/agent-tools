"""Suggest every script directly under tests/ and scripts/ as a check."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agent_run.repository import Repository

if TYPE_CHECKING:
    from agent_run.detectors import Candidate, SkippedFile


def detect_shell_checks(repository: Repository) -> tuple[Candidate | SkippedFile, ...]:
    """Suggest each script directly under tests/ and scripts/.

    A .sh file runs with Bash, and an executable file without a suffix runs
    directly. Other files are reported as skipped; subfolders are ignored. Each
    candidate runs from the repository root and is named after the file without
    its suffix.

    Args:
        repository: The repository to look in.

    Returns:
        Scripts and skipped files in tests/ in name order, then in scripts/.
        Listing tests/ first means a test script keeps its name when scripts/
        has one with the same name.
    """
    # Import inside the function to avoid a circular import, because the detectors
    # package imports this module to build its list of detectors.
    from agent_run.detectors import Candidate, SkippedFile

    candidates: list[Candidate | SkippedFile] = []

    for directory_name in ("tests", "scripts"):
        directory = repository.root / directory_name
        if not directory.is_dir():
            continue

        for script in sorted(directory.iterdir()):
            if not script.is_file():
                continue

            relative_path = script.relative_to(repository.root).as_posix()
            if script.suffix == ".sh":
                argv = ("bash", relative_path)
            elif not script.suffix and os.access(script, os.X_OK):
                argv = (relative_path,)
            else:
                candidates.append(
                    SkippedFile(
                        "shell",
                        relative_path,
                        "neither a .sh file nor an executable without a suffix",
                    )
                )
                continue

            candidates.append(
                Candidate(
                    name=script.stem,
                    argv=argv,
                    working_directory=".",
                    detector=relative_path,
                )
            )

    return tuple(candidates)
