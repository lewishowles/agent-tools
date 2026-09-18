"""Combine built-in command detectors into a duplicate-free preview."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_run.repository import Repository


@dataclass(frozen=True)
class Candidate:
    """Describe one command suggested by a detector.

    Attributes:
        name: Name the command would use when registered.
        argv: Argument array the command would run.
        working_directory: Repository-relative directory, using `.` for the root.
        detector: Name of the detector that produced the suggestion.
    """

    name: str
    argv: tuple[str, ...]
    working_directory: str
    detector: str


class Detector(Protocol):
    """Return command candidates found in one repository."""

    def __call__(self, repository: Repository) -> Sequence[Candidate]:
        """Return the candidates found in the given repository."""


@dataclass(frozen=True)
class CandidateCollection:
    """Hold accepted candidates and duplicate candidates skipped by name.

    Attributes:
        candidates: First candidate seen for each command name.
        skipped: Later candidates whose names were already accepted.
    """

    candidates: tuple[Candidate, ...]
    skipped: tuple[Candidate, ...]


# Built-in detectors, run in this order. Each one is listed here by hand; agent-run
# never loads detectors from other packages.
DETECTORS: tuple[Detector, ...] = ()


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
    skipped: list[Candidate] = []
    seen_names: set[str] = set()

    for detector in detectors:
        for candidate in detector(repository):
            if candidate.name in seen_names:
                skipped.append(candidate)
                continue

            seen_names.add(candidate.name)
            candidates.append(candidate)

    return CandidateCollection(tuple(candidates), tuple(skipped))
