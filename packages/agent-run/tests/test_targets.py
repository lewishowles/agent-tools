"""Tests for resolving named-command file targets."""

import subprocess
from pathlib import Path

import pytest
from agent_run.repository import Repository
from agent_run.targets import TargetError, resolve_file_targets


def _initialise_repository(path: Path) -> Path:
    """Create an empty temporary Git repository for a target test."""
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)

    return path


def _repository(path: Path) -> Repository:
    """Return a repository identity suitable for direct target tests."""
    return Repository(root=path.resolve(), id="repository-id")


def test_resolve_file_targets_deduplicates_and_renders_from_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Targets keep input order and become paths relative to the run directory."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    (root / "src").mkdir()
    source_file = root / "src" / "main.py"
    source_file.write_text("print('ok')")
    (root / "alias.py").symlink_to(source_file)

    monkeypatch.chdir(root / "tools")

    targets = resolve_file_targets(
        _repository(root),
        root / "tools",
        ["../src/main.py", "../src/main.py", "../alias.py"],
    )

    assert targets == ("../src/main.py",)


def test_resolve_file_targets_rejects_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing target is rejected before the command starts."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    with pytest.raises(TargetError, match="does not exist"):
        resolve_file_targets(_repository(root), root, ["missing.py"])


def test_resolve_file_targets_rejects_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory is not accepted as a file target."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "directory").mkdir()
    monkeypatch.chdir(root)

    with pytest.raises(TargetError, match="regular file"):
        resolve_file_targets(_repository(root), root, ["directory"])


def test_resolve_file_targets_rejects_paths_outside_repository(tmp_path: Path) -> None:
    """A target that resolves outside the repository is rejected."""
    root = _initialise_repository(tmp_path / "repository")
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("print('outside')")

    with pytest.raises(TargetError, match="inside the repository"):
        resolve_file_targets(_repository(root), root, [str(outside_file)])
