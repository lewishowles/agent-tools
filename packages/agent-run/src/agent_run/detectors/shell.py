"""Suggest the shell scripts under tests/ and scripts/ that are safe checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_run.repository import Repository

if TYPE_CHECKING:
    from agent_run.detectors import Candidate


# The only scripts/ names suggested as checks. Other scripts there, such as sync or
# build, may write to the repository, so they are never suggested.
SCRIPT_CHECK_NAMES = ("validate", "test", "check", "lint")


def detect_shell_checks(repository: Repository) -> tuple[Candidate, ...]:
    """Suggest each .sh file directly under tests/ and each known check script.

    Every candidate runs the script with bash from the repository root and is
    named after the file without its .sh suffix.

    Args:
        repository: The repository to look in.

    Returns:
        The test scripts in name order, then the scripts/ checks in the order of
        SCRIPT_CHECK_NAMES, or an empty tuple when there are none.
    """
    # Import inside the function to avoid a circular import, because the detectors
    # package imports this module to build its list of detectors.
    from agent_run.detectors import Candidate

    candidates: list[Candidate] = []
    tests_directory = repository.root / "tests"

    for test_script in sorted(tests_directory.glob("*.sh")):
        if not test_script.is_file():
            continue

        relative_path = test_script.relative_to(repository.root).as_posix()
        candidates.append(
            Candidate(
                name=test_script.stem,
                argv=("bash", relative_path),
                working_directory=".",
                detector=relative_path,
            )
        )

    scripts_directory = repository.root / "scripts"

    for script_name in SCRIPT_CHECK_NAMES:
        script = scripts_directory / f"{script_name}.sh"
        if not script.is_file():
            continue

        relative_path = script.relative_to(repository.root).as_posix()
        candidates.append(
            Candidate(
                name=script_name,
                argv=("bash", relative_path),
                working_directory=".",
                detector=relative_path,
            )
        )

    return tuple(candidates)
