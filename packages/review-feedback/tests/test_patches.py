"""Tests for creating, checking and refreshing review patches in throwaway Git repositories."""

import hashlib
import json
import subprocess
import sys

import pytest

from review_feedback import patches


def git(root, *arguments):
	"""Run a Git command in a test repository and return its trimmed output."""
	return subprocess.run(
		["git", *arguments], cwd=root, check=True, capture_output=True, text=True
	).stdout.strip()


def write_plan(path, proposals):
	"""Write `proposals` to `path` as a patch plan."""
	path.write_text(json.dumps({"proposals": proposals}), encoding="utf-8")


@pytest.fixture
def patch_repo(tmp_path):
	"""Create a repository with two separate changes in one file, a new file and a deleted file.

	Returns the repository, a plan giving each change its own proposal, the output folder,
	the changed file's lines and the proposals.
	"""
	root = tmp_path / "repo"
	root.mkdir()
	git(root, "init", "-q")
	git(root, "config", "user.email", "review-patches@example.test")
	git(root, "config", "user.name", "Review patches tests")

	original = [f"line {index}\n" for index in range(40)]
	(root / "tracked.txt").write_text("".join(original), encoding="utf-8")
	(root / "deleted.txt").write_text("remove me\n", encoding="utf-8")
	git(root, "add", ".")
	git(root, "commit", "-qm", "initial")

	changed = original.copy()
	changed[2] = "first change\n"
	changed[30] = "second change\n"
	(root / "tracked.txt").write_text("".join(changed), encoding="utf-8")
	(root / "deleted.txt").unlink()
	(root / "new.txt").write_text("new content\n", encoding="utf-8")

	proposals = [
		{
			"id": "first",
			"title": "First hunk",
			"changes": [{"path": "tracked.txt", "hunks": [0]}],
		},
		{
			"id": "second",
			"title": "Second hunk",
			"changes": [{"path": "tracked.txt", "hunks": [1]}],
		},
		{"id": "new", "title": "New file", "changes": [{"path": "new.txt"}]},
		{"id": "delete", "title": "Deleted file", "changes": [{"path": "deleted.txt"}]},
	]
	plan_path = tmp_path / "plan.json"
	write_plan(plan_path, proposals)

	return root, plan_path, tmp_path / "patches", changed, proposals


def create(patch_repo, *, staged_policy="refuse"):
	"""Generate patches for the test repository's plan."""
	root, plan_path, directory, _, _ = patch_repo

	return patches.generate(root, plan_path, directory, staged_policy=staged_policy)


def test_create_preserves_patch_and_metadata_contract(patch_repo):
	"""Creating patches records each proposal's hunks, file hashes and base commit."""
	root, plan_path, directory, _, _ = patch_repo

	manifest = create(patch_repo)

	assert manifest["format_version"] == 1
	assert manifest["base"] == git(root, "rev-parse", "HEAD")
	assert manifest["context_lines"] == 10
	assert manifest["staged_policy"] == "refuse"
	assert manifest["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest()
	assert [proposal["id"] for proposal in manifest["proposals"]] == [
		"first",
		"second",
		"new",
		"delete",
	]
	assert json.loads((directory / "manifest.json").read_text()) == manifest
	assert "new file mode" in (directory / "new.patch").read_text()
	assert "deleted file mode" in (directory / "delete.patch").read_text()
	assert "first change" in (directory / "first.patch").read_text()
	assert "second change" not in (directory / "first.patch").read_text()

	for proposal in manifest["proposals"]:
		metadata = json.loads((directory / proposal["metadata"]).read_text())

		assert metadata["format_version"] == 1
		assert metadata["base"] == manifest["base"]
		assert metadata["freshness"] == "fresh"
		assert metadata["apply_check"] == "passed"
		assert metadata["patch"]["sha256"] == proposal["sha256"]
		assert (
			metadata["changes"][0]["base_sha256"]
			!= metadata["changes"][0]["worktree_sha256"]
		)


def test_check_reports_fresh_and_changed_worktree(patch_repo, capsys):
	"""A check passes after creating patches and fails once a changed file stops matching."""
	root, plan_path, directory, _, _ = patch_repo
	create(patch_repo)

	assert patches.check_directory(root, directory, plan_path) == 0
	assert "fresh: first" in capsys.readouterr().out

	(root / "tracked.txt").write_text("feedback changed\n", encoding="utf-8")

	assert patches.check_directory(root, directory, plan_path) == 1
	assert "worktree_sha256 changed" in capsys.readouterr().out


def test_check_detects_plan_and_patch_changes(patch_repo, capsys):
	"""A check fails when the plan or a patch file changes after creation."""
	root, plan_path, directory, _, proposals = patch_repo
	create(patch_repo)

	write_plan(plan_path, list(reversed(proposals)))

	assert patches.check_directory(root, directory, plan_path) == 1
	assert "stale: plan changed" in capsys.readouterr().out

	write_plan(plan_path, proposals)
	(directory / "first.patch").write_bytes(b"tampered\n")

	assert patches.check_directory(root, directory, plan_path) == 1
	assert "patch hash changed" in capsys.readouterr().out


def test_refresh_rewrites_only_selected_proposal(patch_repo):
	"""Refreshing one proposal rewrites its patch and leaves another proposal's files alone."""
	root, plan_path, directory, changed, _ = patch_repo
	create(patch_repo)
	second_patch = (directory / "second.patch").read_bytes()
	second_metadata = (directory / "second.json").read_bytes()

	changed[2] = "updated first change\n"
	(root / "tracked.txt").write_text("".join(changed), encoding="utf-8")

	patches.generate(
		root, plan_path, directory, staged_policy="refuse", refresh_id="first"
	)

	assert "updated first change" in (directory / "first.patch").read_text()
	assert (directory / "second.patch").read_bytes() == second_patch
	assert (directory / "second.json").read_bytes() == second_metadata
	assert patches.check_directory(root, directory, plan_path) == 1


def test_check_recovers_after_restoring_inputs(patch_repo):
	"""A stale check becomes fresh again when the changed file is restored."""
	root, plan_path, directory, changed, _ = patch_repo
	create(patch_repo)

	(root / "tracked.txt").write_text("temporary drift\n", encoding="utf-8")

	assert patches.check_directory(root, directory, plan_path) == 1

	(root / "tracked.txt").write_text("".join(changed), encoding="utf-8")

	assert patches.check_directory(root, directory, plan_path) == 0


def test_staged_policy_refuses_or_includes_index_changes(patch_repo):
	"""Staged changes are refused by default and accepted with "include", without changing the index."""
	root, _, directory, _, _ = patch_repo
	git(root, "add", "tracked.txt")

	with pytest.raises(patches.PatchError, match="index contains staged changes"):
		create(patch_repo)

	manifest = create(patch_repo, staged_policy="include")

	assert manifest["staged_policy"] == "include"
	assert (
		json.loads((directory / "first.json").read_text())["staged_policy"] == "include"
	)
	assert git(root, "diff", "--cached", "--name-only") == "tracked.txt"


def test_rejects_overlapping_and_missing_hunks(patch_repo):
	"""Creating patches fails, writing nothing, when a hunk is in two proposals or in none."""
	root, plan_path, directory, _, _ = patch_repo
	write_plan(
		plan_path,
		[
			{"id": "one", "changes": [{"path": "tracked.txt", "hunks": [0]}]},
			{"id": "two", "changes": [{"path": "tracked.txt", "hunks": [0]}]},
		],
	)

	with pytest.raises(patches.PatchError, match="more than one proposal"):
		create(patch_repo)

	write_plan(
		plan_path, [{"id": "one", "changes": [{"path": "tracked.txt", "hunks": [0]}]}]
	)

	with pytest.raises(patches.PatchError, match="missing from the plan"):
		create(patch_repo)

	assert not directory.exists()
	assert git(root, "diff", "--cached", "--name-only") == ""


def test_subcommands_preserve_output_and_exit_codes(patch_repo, monkeypatch, capsys):
	"""Each subcommand prints its expected output and returns its expected exit status."""
	root, plan_path, directory, _, _ = patch_repo
	monkeypatch.chdir(root)

	create_args = patches.parse_arguments(
		["create", "--plan", str(plan_path), "--output-dir", str(directory)]
	)

	assert patches.run(create_args) == 0
	assert json.loads(capsys.readouterr().out)["format_version"] == 1

	check_args = patches.parse_arguments(
		["check", str(directory), "--plan", str(plan_path)]
	)
	assert patches.run(check_args) == 0
	assert "fresh: first" in capsys.readouterr().out

	refresh_args = patches.parse_arguments(
		["refresh", "first", "--plan", str(plan_path), "--output-dir", str(directory)]
	)
	assert patches.run(refresh_args) == 0
	assert json.loads(capsys.readouterr().out)["proposals"][0]["id"] == "first"

	(root / "tracked.txt").write_text("changed again\n", encoding="utf-8")

	assert patches.run(check_args) == 1
	assert "stale:" in capsys.readouterr().out


def test_check_uses_explicit_root_from_another_directory(
	patch_repo, monkeypatch, capsys
):
	"""`check --root` finds the repository when run from outside it."""
	root, plan_path, directory, _, _ = patch_repo
	create(patch_repo)
	monkeypatch.chdir(root.parent)
	arguments = patches.parse_arguments(
		[
			"check",
			str(directory),
			"--plan",
			str(plan_path),
			"--root",
			str(root),
		]
	)

	assert patches.run(arguments) == 0
	assert "fresh: first" in capsys.readouterr().out


def test_main_reports_incomplete_plan_as_error(patch_repo, monkeypatch, capsys):
	"""Return exit code 2 and an error when the plan omits a changed hunk."""
	root, plan_path, directory, _, proposals = patch_repo
	write_plan(
		plan_path, [proposal for proposal in proposals if proposal["id"] != "second"]
	)
	monkeypatch.setattr(
		sys,
		"argv",
		[
			"review-patches",
			"create",
			"--root",
			str(root),
			"--plan",
			str(plan_path),
			"--output-dir",
			str(directory),
		],
	)

	exit_code = patches.main()
	output = capsys.readouterr()

	assert exit_code == 2
	assert output.err.startswith("error: changed units are missing")
