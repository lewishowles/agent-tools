"""Tests for local repository discovery and identity."""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from agent_run.cli import main
from agent_run.repository import (
    RepositoryError,
    RepositoryUninitialisedError,
    create_repository_id,
    find_repository_root,
    get_repository_id,
    identify_repository,
)


def _create_repository(path: Path) -> Path:
    """Create an empty temporary Git repository without changing this checkout."""
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)

    return path


def _initialise_repository(path: Path) -> Path:
    """Create a temporary Git repository with an agent-run ID."""
    root = _create_repository(path)
    create_repository_id(root)

    return root


def test_find_repository_root_from_nested_directory(tmp_path: Path) -> None:
    """Discovery returns the repository root from a nested directory."""
    root = _create_repository(tmp_path / "repository")
    nested = root / "nested" / "directory"
    nested.mkdir(parents=True)

    assert find_repository_root(nested) == root.resolve()


def test_find_repository_root_rejects_missing_start_directory(tmp_path: Path) -> None:
    """Discovery reports a missing starting directory before running Git."""
    missing_path = tmp_path / "missing"

    with pytest.raises(RepositoryError, match="is not a directory"):
        find_repository_root(missing_path)


def test_get_repository_id_returns_none_when_it_is_missing(tmp_path: Path) -> None:
    """Reading an uninitialised repository ID does not create one."""
    root = _create_repository(tmp_path / "repository")

    assert get_repository_id(root) is None


def test_identify_repository_rejects_a_missing_repository_id(tmp_path: Path) -> None:
    """Identifying an uninitialised repository does not create an ID."""
    root = _create_repository(tmp_path / "repository")

    with pytest.raises(
        RepositoryUninitialisedError,
        match="This repository has no agent-run ID yet. Run agent-run init here once.",
    ):
        identify_repository(root)

    assert get_repository_id(root) is None


def test_create_repository_id_writes_once_and_reuses_it(tmp_path: Path) -> None:
    """Creating the ID stores it in local Git config and reuses it afterwards."""
    root = _create_repository(tmp_path / "repository")

    first_id = create_repository_id(root)
    second_id = create_repository_id(root)
    configured_id = subprocess.run(
        ["git", "config", "--local", "--get", "agent-run.repository-id"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert first_id == second_id == configured_id


@pytest.mark.parametrize(
    ("failure", "stderr", "expected_message"),
    [
        ("read", "", "Git could not read the local repository ID."),
        (
            "read",
            " fatal: could not read config \n",
            "Git could not read the local repository ID. fatal: could not read config",
        ),
        ("write", "", "Git could not write the local repository ID."),
        (
            "write",
            " fatal: could not write config \n",
            "Git could not write the local repository ID. fatal: could not write config",
        ),
    ],
)
def test_repository_id_errors_include_git_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    stderr: str,
    expected_message: str,
) -> None:
    """Repository ID errors add Git's trimmed error text when Git printed any."""
    root = tmp_path / "repository"
    root.mkdir()
    if failure == "read":
        git_results = [
            subprocess.CompletedProcess(["git"], 2, stdout="", stderr=stderr)
        ]
    else:
        git_results = [
            subprocess.CompletedProcess(["git"], 1, stdout="", stderr=""),
            subprocess.CompletedProcess(["git"], 2, stdout="", stderr=stderr),
        ]

    def run_git(
        _root: Path, _arguments: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        """Return the next simulated Git result."""
        return git_results.pop(0)

    monkeypatch.setattr("agent_run.repository._run_git", run_git)

    with pytest.raises(RepositoryError) as error:
        get_repository_id(root) if failure == "read" else create_repository_id(root)

    assert str(error.value) == expected_message


def test_cloned_repository_gets_a_different_id(tmp_path: Path) -> None:
    """A clone receives its own ID because local Git config is not cloned."""
    source = _create_repository(tmp_path / "source")
    source_id = create_repository_id(source)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(source), str(clone)], check=True)

    clone_id = create_repository_id(clone)

    assert clone_id != source_id


def test_outside_repository_returns_environment_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository command maps a non-repository directory to environment."""
    monkeypatch.chdir(tmp_path)

    exit_code = main(["repository"])

    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert captured.err.startswith("Error: ")


def test_missing_git_returns_environment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The repository command maps missing Git to environment."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    exit_code = main(["repository"])

    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert "Git is not available" in captured.err


def test_repository_subcommand_supports_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository command prints its identity in text mode."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    text_exit_code = main(["repository"])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert text_output.err == ""
    assert f"root: {root.resolve()}" in text_output.out
    assert "id: " in text_output.out


def test_repository_subcommand_supports_json_after_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository command accepts `--json` after its subcommand."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    exit_code = main(["repository", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert result["ok"] is True
    assert result["data"]["root"] == str(root.resolve())
    assert result["data"]["id"]


def test_repository_subcommand_help_uses_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repository help is returned inside the JSON success envelope."""
    exit_code = main(["--json", "repository", "--help"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert "Identify the local Git repository." in result["data"]["help"]
    assert captured.err == ""


def test_repository_subcommand_usage_error_uses_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repository argument errors use the JSON usage envelope."""
    with pytest.raises(SystemExit) as error:
        main(["--json", "repository", "--unknown"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "unrecognized arguments" in result["error"]["message"]
    assert "agent-run: error: unrecognized arguments" in captured.err


def test_repository_json_environment_error_uses_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repository environment errors keep stdout to one JSON envelope."""
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--json", "repository"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert result["ok"] is False
    assert result["error"]["code"] == "environment"
    assert captured.err == ""
