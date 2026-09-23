"""Tests for command detection and its read-only CLI preview."""

import json
import subprocess
from pathlib import Path

import pytest
from agent_run import cli, detectors
from agent_run.cli import main
from agent_run.commands import add_command, list_commands
from agent_run.database import connect_database
from agent_run.detectors import (
    Candidate,
    CandidateCollection,
    SkippedCandidate,
    SkippedFile,
    collect_candidates,
)
from agent_run.detectors.package_json import detect_package_json_scripts
from agent_run.detectors.pyproject import detect_pyproject_checks
from agent_run.detectors.shell import detect_shell_checks
from agent_run.detectors.swift import detect_swift_checks
from agent_run.repository import Repository, create_repository_id, identify_repository


def _initialise_repository(path: Path) -> Path:
    """Create a temporary Git repository with an agent-run ID."""
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    create_repository_id(path)

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
    assert collection.skipped == (SkippedCandidate(duplicate, "duplicate name"),)


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


def test_package_json_detector_marks_browser_runner_scripts(
    tmp_path: Path,
) -> None:
    """Package scripts that start browser runners are marked manual once."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "browser": "npx playwright test",
                    "e2e": "npm run lint && cypress run",
                    "plain": "echo playwright test",
                }
            }
        ),
        encoding="utf-8",
    )

    candidates = detect_package_json_scripts(_repository(root))

    assert [candidate.manual for candidate in candidates] == [True, True, False]


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


@pytest.mark.parametrize(
    ("uv_lock", "runner"),
    [
        (True, ("uv", "run")),
        (False, ()),
    ],
)
def test_pyproject_detector_returns_configured_checks(
    tmp_path: Path,
    uv_lock: bool,
    runner: tuple[str, ...],
) -> None:
    """Configured Python checks use uv only when the root has uv.lock."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n\n[tool.ruff]\nline-length = 88\n",
        encoding="utf-8",
    )
    if uv_lock:
        (root / "uv.lock").touch()

    candidates = detect_pyproject_checks(_repository(root))

    assert candidates == (
        Candidate("pytest", (*runner, "pytest"), ".", "pyproject.toml"),
        Candidate("ruff", (*runner, "ruff", "check"), ".", "pyproject.toml"),
    )


def test_pyproject_detector_only_reads_the_root_file(tmp_path: Path) -> None:
    """Nested Python configuration does not create root candidates."""
    root = _initialise_repository(tmp_path / "repository")
    nested = root / "packages" / "python"
    nested.mkdir(parents=True)
    (nested / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n\n[tool.ruff]\n", encoding="utf-8"
    )

    assert detect_pyproject_checks(_repository(root)) == ()


@pytest.mark.parametrize(
    "pyproject",
    [
        "",
        "[tool.pytest]\n",
        "[tool.ruff\n",
        "not toml",
    ],
)
def test_pyproject_detector_ignores_missing_or_unusable_configuration(
    tmp_path: Path,
    pyproject: str,
) -> None:
    """Only the supported configuration tables produce candidates."""
    root = _initialise_repository(tmp_path / "repository")
    if pyproject:
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    candidates = detect_pyproject_checks(_repository(root))

    assert candidates == ()


@pytest.mark.parametrize(
    ("package", "swift_format", "expected"),
    [
        (
            True,
            False,
            (
                Candidate("swift-build", ("swift", "build"), ".", "Package.swift"),
                Candidate("swift-test", ("swift", "test"), ".", "Package.swift"),
            ),
        ),
        (
            False,
            True,
            (
                Candidate(
                    "swift-format",
                    ("swift-format", "lint", "--recursive", "."),
                    ".",
                    ".swift-format",
                ),
            ),
        ),
        (
            True,
            True,
            (
                Candidate("swift-build", ("swift", "build"), ".", "Package.swift"),
                Candidate("swift-test", ("swift", "test"), ".", "Package.swift"),
                Candidate(
                    "swift-format",
                    ("swift-format", "lint", "--recursive", "."),
                    ".",
                    ".swift-format",
                ),
            ),
        ),
        (False, False, ()),
    ],
)
def test_swift_detector_returns_supported_root_checks(
    tmp_path: Path,
    package: bool,
    swift_format: bool,
    expected: tuple[Candidate, ...],
) -> None:
    """Root Swift files produce only their supported check candidates."""
    root = _initialise_repository(tmp_path / "repository")
    nested = root / "nested"
    nested.mkdir()
    (nested / "Package.swift").touch()
    (nested / ".swift-format").touch()

    if package:
        (root / "Package.swift").write_text(
            "// swift-tools-version: 5.9\n", encoding="utf-8"
        )
    if swift_format:
        (root / ".swift-format").write_text("{}\n", encoding="utf-8")

    candidates = detect_swift_checks(_repository(root))

    assert candidates == expected


def test_shell_detector_returns_direct_scripts(tmp_path: Path) -> None:
    """Scripts in tests/ and scripts/ become candidates; other files are ignored."""
    root = _initialise_repository(tmp_path / "repository")
    tests_directory = root / "tests"
    scripts_directory = root / "scripts"
    nested_tests = tests_directory / "nested"
    tests_directory.mkdir()
    scripts_directory.mkdir()
    nested_tests.mkdir()

    (tests_directory / "integration.sh").touch()
    (tests_directory / "unit.sh").touch()
    (tests_directory / "executable").touch(mode=0o755)
    (tests_directory / "notes").touch()
    (tests_directory / "data.txt").touch()
    (nested_tests / "nested.sh").touch()
    (scripts_directory / "validate.sh").touch()
    (scripts_directory / "test.sh").touch()
    (scripts_directory / "check.sh").touch()
    (scripts_directory / "lint.sh").touch()
    (scripts_directory / "sync.sh").touch()
    (scripts_directory / "build.sh").touch()
    (scripts_directory / "deploy").touch(mode=0o755)
    (scripts_directory / "notes").touch()
    (scripts_directory / "data.py").touch(mode=0o755)

    candidates = detect_shell_checks(_repository(root))

    assert candidates == (
        SkippedFile(
            "shell",
            "tests/data.txt",
            "neither a .sh file nor an executable without a suffix",
        ),
        Candidate("executable", ("tests/executable",), ".", "tests/executable"),
        Candidate(
            "integration",
            ("bash", "tests/integration.sh"),
            ".",
            "tests/integration.sh",
        ),
        SkippedFile(
            "shell",
            "tests/notes",
            "neither a .sh file nor an executable without a suffix",
        ),
        Candidate("unit", ("bash", "tests/unit.sh"), ".", "tests/unit.sh"),
        Candidate("build", ("bash", "scripts/build.sh"), ".", "scripts/build.sh"),
        Candidate("check", ("bash", "scripts/check.sh"), ".", "scripts/check.sh"),
        SkippedFile(
            "shell",
            "scripts/data.py",
            "neither a .sh file nor an executable without a suffix",
        ),
        Candidate("deploy", ("scripts/deploy",), ".", "scripts/deploy"),
        Candidate("lint", ("bash", "scripts/lint.sh"), ".", "scripts/lint.sh"),
        SkippedFile(
            "shell",
            "scripts/notes",
            "neither a .sh file nor an executable without a suffix",
        ),
        Candidate("sync", ("bash", "scripts/sync.sh"), ".", "scripts/sync.sh"),
        Candidate("test", ("bash", "scripts/test.sh"), ".", "scripts/test.sh"),
        Candidate(
            "validate",
            ("bash", "scripts/validate.sh"),
            ".",
            "scripts/validate.sh",
        ),
    )


def test_shell_detector_prefers_tests_for_duplicate_names(tmp_path: Path) -> None:
    """A test script wins when a script in scripts/ has the same name."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tests").mkdir()
    (root / "scripts").mkdir()
    (root / "tests" / "check.sh").touch()
    (root / "scripts" / "check.sh").touch()

    collection = collect_candidates(_repository(root), detectors=(detect_shell_checks,))

    assert collection.candidates == (
        Candidate("check", ("bash", "tests/check.sh"), ".", "tests/check.sh"),
    )
    assert collection.skipped == (
        SkippedCandidate(
            Candidate("check", ("bash", "scripts/check.sh"), ".", "scripts/check.sh"),
            "duplicate name",
        ),
    )


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
    skipped_file = SkippedFile(
        "shell",
        "tests/data.py",
        "neither a .sh file nor an executable without a suffix",
    )

    def fake_detector(_: Repository) -> tuple[Candidate | SkippedFile, ...]:
        return (registered_candidate, new_candidate, skipped_candidate, skipped_file)

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
                    "reason": "duplicate name",
                },
                {
                    "detector": "shell",
                    "path": "tests/data.py",
                    "reason": "neither a .sh file nor an executable without a suffix",
                },
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
    skipped_file = SkippedFile(
        "shell",
        "tests/data.py",
        "neither a .sh file nor an executable without a suffix",
    )

    def fake_detector(_: Repository) -> tuple[Candidate | SkippedFile, ...]:
        return (candidate, skipped_candidate, skipped_file)

    monkeypatch.setattr(detectors, "DETECTORS", (fake_detector,))

    exit_code = main(["detect"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "name: test" in captured.out
    assert "working directory: ." in captured.out
    assert 'argv: ["pytest"]' in captured.out
    assert "detector: fake" in captured.out
    assert "registered: false" in captured.out
    assert "Skipped candidates:" in captured.out
    assert (
        'detector: other; name: test; argv: ["uv", "run", "pytest"]; reason: duplicate name'
        in captured.out
    )
    assert (
        "detector: shell; path: tests/data.py; reason: neither a .sh file nor an executable without a suffix"
        in captured.out
    )
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
