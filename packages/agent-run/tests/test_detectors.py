"""Tests for command detection and its read-only CLI preview."""

import json
import subprocess
from pathlib import Path

from agent_run import detectors
from agent_run.cli import main
from agent_run.commands import add_command
from agent_run.database import connect_database
from agent_run.detectors import Candidate, CandidateCollection, collect_candidates
from agent_run.repository import Repository, identify_repository


def _initialise_repository(path: Path) -> Path:
    """Create an empty temporary Git repository for a detector test."""
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)

    return path


def _repository(path: Path) -> Repository:
    """Return a repository identity suitable for direct detector tests."""
    return Repository(root=path.resolve(), id="repository-id")


def test_collect_candidates_returns_an_empty_collection_without_detectors(
    tmp_path: Path,
) -> None:
    """No detectors produce no accepted or skipped candidates."""
    repository = _repository(_initialise_repository(tmp_path / "repository"))

    collection = collect_candidates(repository)

    assert collection == CandidateCollection(candidates=(), skipped=())


def test_collect_candidates_keeps_the_first_duplicate_name(
    tmp_path: Path,
) -> None:
    """Candidates with a name seen earlier are reported as skipped."""
    repository = _repository(_initialise_repository(tmp_path / "repository"))
    first = Candidate("test", ("pytest",), ".", "first")
    duplicate = Candidate("test", ("uv", "run", "pytest"), "tools", "second")

    def first_detector(_: Repository) -> tuple[Candidate, ...]:
        return (first,)

    def second_detector(_: Repository) -> tuple[Candidate, ...]:
        return (duplicate,)

    collection = collect_candidates(repository, (first_detector, second_detector))

    assert collection.candidates == (first,)
    assert collection.skipped == (duplicate,)


def test_detect_reports_no_commands_in_text_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """The empty detector set explains that no commands were found."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    monkeypatch.setattr(detectors, "DETECTORS", ())

    exit_code = main(["detect"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "No commands detected.\n"
    assert captured.err == ""


def test_detect_reports_candidates_in_json_with_registration_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """JSON preview includes candidate fields and existing registration state."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))
    registered_candidate = Candidate("test", ("pytest",), ".", "fake")
    new_candidate = Candidate("lint", ("ruff", "check"), "tools", "fake")
    skipped_candidate = Candidate("test", ("uv", "run", "pytest"), ".", "other")

    def fake_detector(_: Repository) -> tuple[Candidate, ...]:
        return (registered_candidate, new_candidate, skipped_candidate)

    monkeypatch.setattr(detectors, "DETECTORS", (fake_detector,))
    connection = connect_database()
    try:
        add_command(
            connection,
            identify_repository(),
            "test",
            ["pytest"],
        )
    finally:
        connection.close()

    exit_code = main(["detect", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert result == {
        "ok": True,
        "data": {
            "candidates": [
                {
                    "name": "test",
                    "working_directory": ".",
                    "argv": ["pytest"],
                    "detector": "fake",
                    "registered": True,
                },
                {
                    "name": "lint",
                    "working_directory": "tools",
                    "argv": ["ruff", "check"],
                    "detector": "fake",
                    "registered": False,
                },
            ],
            "skipped": [
                {
                    "name": "test",
                    "working_directory": ".",
                    "argv": ["uv", "run", "pytest"],
                    "detector": "other",
                    "registered": True,
                }
            ],
        },
    }


def test_detect_reports_candidate_registration_in_text_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Text preview shows each candidate and whether it is registered."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    candidate = Candidate("test", ("pytest",), ".", "fake")
    skipped_candidate = Candidate("test", ("uv", "run", "pytest"), ".", "other")

    def fake_detector(_: Repository) -> tuple[Candidate, ...]:
        return (candidate, skipped_candidate)

    monkeypatch.setattr(detectors, "DETECTORS", (fake_detector,))

    exit_code = main(["detect"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "name: test" in captured.out
    assert "working directory: ." in captured.out
    assert 'argv: ["pytest"]' in captured.out
    assert "detector: fake" in captured.out
    assert "registered: false" in captured.out
    assert "Skipped duplicate candidates:" in captured.out
    assert 'detector: other; name: test; argv: ["uv", "run", "pytest"]' in captured.out
    assert captured.err == ""
