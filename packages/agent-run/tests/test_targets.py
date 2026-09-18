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
        [],
    )

    assert targets == ("../src/main.py",)


def test_resolve_file_targets_rejects_missing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing target is rejected before the command starts."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)

    with pytest.raises(TargetError, match="does not exist"):
        resolve_file_targets(_repository(root), root, ["missing.py"], [])


def test_resolve_file_targets_rejects_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory is not accepted as a file target."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "directory").mkdir()
    monkeypatch.chdir(root)

    with pytest.raises(TargetError, match="regular file"):
        resolve_file_targets(_repository(root), root, ["directory"], [])


def test_resolve_file_targets_rejects_paths_outside_repository(tmp_path: Path) -> None:
    """A target that resolves outside the repository is rejected."""
    root = _initialise_repository(tmp_path / "repository")
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("print('outside')")

    with pytest.raises(TargetError, match="inside the repository"):
        resolve_file_targets(_repository(root), root, [str(outside_file)], [])


def test_resolve_file_targets_expands_globs_in_sorted_order(tmp_path: Path) -> None:
    """A glob returns matching files in sorted repository order."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "src").mkdir()
    (root / "src" / "two.py").write_text("two")
    (root / "src" / "one.py").write_text("one")

    targets = resolve_file_targets(_repository(root), root, [], ["src/*.py"])

    assert targets == ("src/one.py", "src/two.py")


def test_resolve_file_targets_expands_recursive_globs(tmp_path: Path) -> None:
    """A recursive glob includes files at every matching depth."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "src" / "nested").mkdir(parents=True)
    (root / "src" / "top.py").write_text("top")
    (root / "src" / "nested" / "deep.py").write_text("deep")

    targets = resolve_file_targets(_repository(root), root, [], ["src/**/*.py"])

    assert targets == ("src/nested/deep.py", "src/top.py")


def test_resolve_file_targets_excludes_ignored_glob_matches(tmp_path: Path) -> None:
    """Ignored files are not returned by glob expansion."""
    root = _initialise_repository(tmp_path / "repository")
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "ignored.py").write_text("ignored")
    (root / "visible.py").write_text("visible")

    targets = resolve_file_targets(_repository(root), root, [], ["*.py"])

    assert targets == ("visible.py",)


def test_resolve_file_targets_rejects_glob_with_only_ignored_matches(
    tmp_path: Path,
) -> None:
    """A glob with only ignored files is rejected."""
    root = _initialise_repository(tmp_path / "repository")
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "ignored.py").write_text("ignored")

    with pytest.raises(TargetError, match=r"\*\.py"):
        resolve_file_targets(_repository(root), root, [], ["*.py"])


def test_resolve_file_targets_rejects_unmatched_glob(tmp_path: Path) -> None:
    """A glob with no repository matches is rejected."""
    root = _initialise_repository(tmp_path / "repository")

    with pytest.raises(TargetError, match=r"missing/\*\*/\*\.py"):
        resolve_file_targets(_repository(root), root, [], ["missing/**/*.py"])


def test_resolve_file_targets_skips_deleted_tracked_glob_matches(
    tmp_path: Path,
) -> None:
    """A deleted tracked file leaves a glob with no usable matches."""
    root = _initialise_repository(tmp_path / "repository")
    deleted_file = root / "deleted.py"
    deleted_file.write_text("deleted")
    subprocess.run(["git", "add", "deleted.py"], cwd=root, check=True)
    deleted_file.unlink()

    with pytest.raises(TargetError, match=r"\*\.py"):
        resolve_file_targets(_repository(root), root, [], ["*.py"])


def test_resolve_file_targets_keeps_existing_glob_matches_beside_deleted_ones(
    tmp_path: Path,
) -> None:
    """A deleted tracked file is dropped while its existing siblings are kept."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "kept.py").write_text("kept")
    deleted_file = root / "deleted.py"
    deleted_file.write_text("deleted")
    subprocess.run(["git", "add", "deleted.py"], cwd=root, check=True)
    deleted_file.unlink()

    assert resolve_file_targets(_repository(root), root, [], ["*.py"]) == ("kept.py",)


def test_resolve_file_targets_deduplicates_files_and_globs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File paths stay first while duplicate glob matches are removed."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "src").mkdir()
    (root / "src" / "one.py").write_text("one")
    (root / "src" / "two.py").write_text("two")
    monkeypatch.chdir(root)

    targets = resolve_file_targets(
        _repository(root), root, ["src/one.py"], ["src/*.py"]
    )

    assert targets == ("src/one.py", "src/two.py")
