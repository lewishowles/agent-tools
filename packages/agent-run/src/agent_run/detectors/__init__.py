"""Combine built-in command detectors into a duplicate-free preview."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_run.detectors.package_json import detect_package_json_scripts
from agent_run.detectors.pyproject import detect_pyproject_checks
from agent_run.detectors.shell import detect_shell_checks
from agent_run.detectors.swift import detect_swift_checks
from agent_run.repository import Repository


@dataclass(frozen=True)
class Candidate:
    """Describe one command suggested by a detector.

    Attributes:
        name: Name the command would use when registered.
        argv: Argument array the command would run.
        working_directory: Repository-relative directory, using `.` for the root.
        detector: Name of the detector that produced the suggestion.
        manual: Whether the candidate needs a human to run it.
    """

    name: str
    argv: tuple[str, ...]
    working_directory: str
    detector: str
    manual: bool = False


class Detector(Protocol):
    """Return command candidates found in one repository."""

    def __call__(self, repository: Repository) -> Sequence[Candidate | SkippedFile]:
        """Return the candidates found in the given repository."""


@dataclass(frozen=True)
class SkippedCandidate:
    """A candidate that detect left out, with the reason.

    Attributes:
        candidate: The candidate as its detector suggested it.
        reason: Why it was left out, such as a name another candidate already uses.
    """

    candidate: Candidate
    reason: str


@dataclass(frozen=True)
class SkippedFile:
    """A file a detector looked at but could not suggest as a command.

    Attributes:
        detector: The name of the detector that found the file.
        path: The file's path relative to the repository root.
        reason: Why the file was not suggested.
    """

    detector: str
    path: str
    reason: str


@dataclass(frozen=True)
class CandidateCollection:
    """Hold accepted candidates and anything detectors left out.

    Attributes:
        candidates: First candidate seen for each command name.
        skipped: Rejected candidates and files, each with a reason.
    """

    candidates: tuple[Candidate, ...]
    skipped: tuple[SkippedCandidate | SkippedFile, ...]


# Built-in detectors, run in this order. Each one is listed here by hand; agent-run
# never loads detectors from other packages.
DETECTORS: tuple[Detector, ...] = (
    detect_package_json_scripts,
    detect_pyproject_checks,
    detect_swift_checks,
    detect_shell_checks,
)


def collect_candidates(
    repository: Repository,
    detectors: Sequence[Detector] | None = None,
) -> CandidateCollection:
    """Run detectors in order and keep the first candidate for each name.

    Args:
        repository: Repository whose files detectors should inspect.
        detectors: Ordered detector callables, or `None` to use the built-in set.

    Returns:
        Accepted candidates and later duplicate candidates marked as skipped.
    """
    if detectors is None:
        detectors = DETECTORS

    candidates: list[Candidate] = []
    skipped: list[SkippedCandidate | SkippedFile] = []
    seen_names: set[str] = set()

    for detector in detectors:
        for candidate in detector(repository):
            # Detectors return the files they could not use alongside their
            # candidates, so only real candidates reach the name check.
            if isinstance(candidate, SkippedFile):
                skipped.append(candidate)
                continue

            if candidate.name in seen_names:
                skipped.append(SkippedCandidate(candidate, "duplicate name"))
                continue

            seen_names.add(candidate.name)
            candidates.append(candidate)

    return CandidateCollection(tuple(candidates), tuple(skipped))
