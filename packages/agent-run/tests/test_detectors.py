"""Tests for command detection and its read-only CLI preview."""

import json
import subprocess
from pathlib import Path

import pytest
from agent_run import cli, detectors
from agent_run.cli import main
from agent_run.commands import add_command, list_commands
from agent_run.database import connect_database
from agent_run.detectors import Candidate, CandidateCollection, collect_candidates
from agent_run.detectors.package_json import detect_package_json_scripts
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


@pytest.mark.parametrize(
    ("lockfile", "runner"),
    [
        ("package-lock.json", ("npm", "run")),
        ("pnpm-lock.yaml", ("pnpm", "run")),
        ("yarn.lock", ("yarn",)),
        ("bun.lock", ("bun", "run")),
        ("bun.lockb", ("bun", "run")),
        (None, ("npm", "run")),
    ],
)
def test_package_json_detector_returns_every_root_script(
    tmp_path: Path,
    lockfile: str | None,
    runner: tuple[str, ...],
) -> None:
    """Root scripts use the runner implied by each supported lockfile."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "deploy": "release",
                    "prepare": "generate",
                    "lint:fix": "lint --fix",
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "components").mkdir()
    (root / "components" / "package.json").write_text(
        json.dumps({"scripts": {"nested": "ignored"}}),
        encoding="utf-8",
    )
    if lockfile is not None:
        (root / lockfile).touch()

    candidates = detect_package_json_scripts(_repository(root))

    assert candidates == (
        Candidate("deploy", (*runner, "deploy"), ".", "package.json"),
        Candidate("prepare", (*runner, "prepare"), ".", "package.json"),
        Candidate("lint:fix", (*runner, "lint:fix"), ".", "package.json"),
    )


def test_package_json_detector_ignores_missing_or_malformed_manifests(
    tmp_path: Path,
) -> None:
    """Repositories without a usable root manifest produce no candidates."""
    root = _initialise_repository(tmp_path / "repository")

    assert detect_package_json_scripts(_repository(root)) == ()

    (root / "package.json").write_text("not json", encoding="utf-8")

    assert detect_package_json_scripts(_repository(root)) == ()

    (root / "package.json").write_text('{"scripts": ["test"]}', encoding="utf-8")

    assert detect_package_json_scripts(_repository(root)) == ()


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
                    "registered": False,
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


def test_detect_adds_named_candidates_and_reports_existing_statuses_in_json(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Named additions save new commands without changing existing commands."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    identical = Candidate("test", ("pytest",), ".", "fake")
    new = Candidate("lint", ("ruff", "check"), "tools", "fake")
    conflict = Candidate("format", ("ruff", "format"), ".", "fake")

    def fake_detector(_: Repository) -> tuple[Candidate, ...]:
        return (identical, new, conflict)

    monkeypatch.setattr(detectors, "DETECTORS", (fake_detector,))
    connection = connect_database()
    try:
        repository = identify_repository()
        add_command(connection, repository, "test", ["pytest"])
        add_command(connection, repository, "format", ["black"])
    finally:
        connection.close()

    exit_code = main(
        [
            "detect",
            "--add",
            "lint",
            "--add",
            "test",
            "--add",
            "format",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert [candidate["name"] for candidate in result["data"]["added"]] == ["lint"]
    assert [
        candidate["name"] for candidate in result["data"]["already_registered"]
    ] == ["test"]
    assert result["data"]["conflicts"] == [
        {
            "name": "format",
            "working_directory": ".",
            "argv": ["ruff", "format"],
            "detector": "fake",
            "registered": False,
            "edit_command": "agent-run edit format --cwd . -- ruff format",
        }
    ]

    connection = connect_database()
    try:
        commands = {
            command.name: command
            for command in list_commands(connection, identify_repository())
        }
    finally:
        connection.close()

    assert commands["lint"].argv == ("ruff", "check")
    assert commands["format"].argv == ("black",)


def test_detect_all_reports_added_commands_and_conflicts_in_text_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """The all option saves new commands and reports unchanged registrations."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    identical = Candidate("test", ("pytest",), ".", "fake")
    new = Candidate("lint", ("ruff", "check"), ".", "fake")
    conflict = Candidate("format", ("ruff", "format"), ".", "fake")

    def fake_detector(_: Repository) -> tuple[Candidate, ...]:
        return (identical, new, conflict)

    monkeypatch.setattr(detectors, "DETECTORS", (fake_detector,))
    connection = connect_database()
    try:
        add_command(connection, identify_repository(), "test", ["pytest"])
        add_command(connection, identify_repository(), "format", ["black"])
    finally:
        connection.close()

    exit_code = main(["detect", "--all"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Added detected commands:\n- lint" in captured.out
    assert "Already registered:\n- test" in captured.out
    assert (
        "Conflicting registered commands:\n- format: edit with "
        "agent-run edit format --cwd . -- ruff format"
    ) in captured.out
    assert captured.err == ""


def test_detect_add_rejects_an_unknown_name_without_saving(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """An unknown detected name fails before any candidate is saved."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    candidate = Candidate("test", ("pytest",), ".", "fake")

    def fake_detector(_: Repository) -> tuple[Candidate, ...]:
        return (candidate,)

    monkeypatch.setattr(detectors, "DETECTORS", (fake_detector,))

    with pytest.raises(SystemExit) as error:
        main(["detect", "--add", "missing"])

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert 'detected command "missing" was not found' in captured.err

    connection = connect_database()
    try:
        commands = list_commands(connection, identify_repository())
    finally:
        connection.close()

    assert commands == []


def test_detect_add_without_a_name_requires_an_interactive_terminal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A bare add explains the non-interactive alternatives."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    with pytest.raises(SystemExit) as error:
        main(["detect", "--add"])

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "pass --add NAME or --all instead" in captured.err


def test_detect_rejects_combining_add_and_all(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """The non-interactive selection modes cannot be combined."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    with pytest.raises(SystemExit) as error:
        main(["detect", "--add", "test", "--all"])

    captured = capsys.readouterr()

    assert error.value.code == 2
    assert "--add and --all cannot be used together" in captured.err


def test_detect_interactive_add_disables_registered_candidates(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """The interactive picker preselects new candidates and disables others."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    new = Candidate("lint", ("ruff", "check"), ".", "fake")
    identical = Candidate("test", ("pytest",), ".", "fake")
    conflict = Candidate("format", ("ruff", "format"), ".", "fake")

    def fake_detector(_: Repository) -> tuple[Candidate, ...]:
        return (new, identical, conflict)

    monkeypatch.setattr(detectors, "DETECTORS", (fake_detector,))
    connection = connect_database()
    try:
        repository = identify_repository()
        add_command(connection, repository, "test", ["pytest"])
        add_command(connection, repository, "format", ["black"])
    finally:
        connection.close()

    captured_choices = []

    class Prompt:
        """Return the newly detected command selected by the fake prompt."""

        def ask(self) -> list[str]:
            return ["lint"]

    def fake_checkbox(message: str, *, choices: list[object]) -> Prompt:
        assert message == "Select detected commands to add:"
        captured_choices.extend(choices)
        return Prompt()

    monkeypatch.setattr(cli, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli.questionary, "checkbox", fake_checkbox)

    exit_code = main(["detect", "--add"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Added detected commands:\n- lint" in captured.out
    assert "Already registered:\n- test" in captured.out
    assert "Conflicting registered commands:" in captured.out
    assert captured.err == ""
    assert captured_choices[0].checked is True
    assert captured_choices[1].disabled == "already registered"
    assert captured_choices[2].disabled == (
        "conflict; use agent-run edit format --cwd . -- ruff format"
    )
