"""Detect Swift checks configured at the repository root."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_run.repository import Repository

if TYPE_CHECKING:
    from agent_run.detectors import Candidate


def detect_swift_checks(repository: Repository) -> tuple[Candidate, ...]:
    """Return candidates for the Swift checks configured at the repository root.

    Args:
        repository: Repository whose root Swift files should be inspected.

    Returns:
        Candidates for Swift package and formatting checks, or an empty tuple when
        the repository has neither supported root file.
    """
    # Import inside the function to avoid a circular import, because the detectors
    # package imports this module to build its list of detectors.
    from agent_run.detectors import Candidate

    candidates: list[Candidate] = []

    if (repository.root / "Package.swift").exists():
        candidates.extend(
            (
                Candidate(
                    name="swift-build",
                    argv=("swift", "build"),
                    working_directory=".",
                    detector="Package.swift",
                ),
                Candidate(
                    name="swift-test",
                    argv=("swift", "test"),
                    working_directory=".",
                    detector="Package.swift",
                ),
            )
        )

    if (repository.root / ".swift-format").exists():
        candidates.append(
            Candidate(
                name="swift-format",
                argv=("swift-format", "lint", "--recursive", "."),
                working_directory=".",
                detector=".swift-format",
            )
        )

    return tuple(candidates)
