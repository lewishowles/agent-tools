"""Check draft behaviour with invented worktrees and progress records."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import review_packet.cli as cli
import review_packet.progress_records as records
from review_packet.brief import render
from review_packet.cli import main
from review_packet.progress_records import (
	Chunk,
	ProgressError,
	RelatedTasks,
	Task,
	TaskChunk,
	read_chunk,
	read_related_tasks,
)
from review_packet.selection import MissingIncludeError, select_files
from review_packet.settings import InvalidSettingsError, read_settings
from review_packet.snapshot import NO_COMMITS, capture


def git(root: Path, *args: str) -> None:
	"""Run a Git command in an invented repository."""
	subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
	"""Create a small committed repository with predictable identities."""
	git(tmp_path, "init", "-q")
	git(tmp_path, "config", "user.email", "review@example.invalid")
	git(tmp_path, "config", "user.name", "Reviewer")
	(tmp_path / "old.txt").write_text("old\n")
	(tmp_path / "delete.txt").write_text("delete\n")
	(tmp_path / "README.md").write_text("Repository\n")
	git(tmp_path, "add", ".")
	git(tmp_path, "commit", "-qm", "initial")
	return tmp_path


def test_snapshot_covers_renames_deletions_untracked_and_base(repo: Path) -> None:
	"""Keep each change kind visible and include commits since a base."""
	base = subprocess.check_output(
		["git", "rev-parse", "HEAD"], cwd=repo, text=True
	).strip()

	git(repo, "mv", "old.txt", "new.txt")
	git(repo, "rm", "delete.txt")
	git(repo, "commit", "-qm", "move and delete")
	(repo / "extra.txt").write_text("new\n")

	snapshot = capture(repo, base)

	assert {(item.path, item.status, item.old_path) for item in snapshot.changes} == {
		("new.txt", "renamed", "old.txt"),
		("delete.txt", "deleted", None),
		("extra.txt", "untracked", None),
	}
	assert snapshot.uncommitted
	assert capture(repo).changes[0].path == "extra.txt"


def test_settings_absent_and_present(repo: Path) -> None:
	"""Use empty settings by default and read invented include values."""
	assert not read_settings(repo).found

	(repo / ".review-packet.toml").write_text(
		'checks = ["test"]\ninclude = ["README.md"]\n'
	)
	settings = read_settings(repo)

	assert settings.found
	assert settings.checks == ("test",)
	assert settings.include == ("README.md",)


def test_progress_uses_temporary_database(
	repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Read an invented task and chunk through progress JSON."""
	database = tmp_path / "progress.db"
	monkeypatch.setenv("AGENTS_PROGRESS_DATABASE", str(database))

	def progress(*arguments: str) -> dict:
		"""Run progress against the temporary database and return its data."""
		result = subprocess.run(
			["progress", *arguments, "--json"],
			cwd=repo,
			capture_output=True,
			text=True,
			check=True,
		)

		return json.loads(result.stdout)["data"]

	progress("project", "init", "--slug", "invented", "--name", "Invented")
	task = progress(
		"task",
		"add",
		"--slug",
		"review",
		"--title",
		"Review",
		"--overview",
		"Invented overview",
		"--done-when",
		"Done",
	)
	chunk = progress(
		"chunk",
		"add",
		"--task",
		task["id"],
		"--title",
		"Draft",
		"--description",
		"Draft a brief",
		"--review-question",
		"Is it complete?",
	)
	progress(
		"task",
		"add",
		"--slug",
		"follow-up",
		"--title",
		"Follow-up",
		"--overview",
		"Pending work",
		"--done-when",
		"Done",
		"--depends-on",
		task["id"],
	)

	assert read_chunk(repo, chunk["id"]).task.title == "Review"
	assert read_related_tasks(repo, task["id"]) == RelatedTasks((), ("Follow-up",))


def test_draft_json_include_and_force(
	repo: Path,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Write an absolute brief path, then refuse replacement without force."""
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "drafts"))
	monkeypatch.chdir(repo)

	assert main(["draft", "chk_example", "--include", "README.md", "--json"]) == 0

	data = json.loads(capsys.readouterr().out)["data"]

	assert Path(data["brief"]).is_absolute()
	assert "README.md" in data["contents"]
	assert Path(data["brief"]).exists()

	assert main(["draft", "chk_example", "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"

	assert main(["draft", "chk_example", "--force", "--json"]) == 0
	assert json.loads(capsys.readouterr().out)["ok"] is True


def test_snapshot_covers_staged_and_unstaged_edit_without_base(repo: Path) -> None:
	"""Include both index and worktree edits when the base defaults to HEAD."""
	(repo / "old.txt").write_text("staged\n")
	git(repo, "add", "old.txt")
	(repo / "old.txt").write_text("staged and unstaged\n")

	snapshot = capture(repo)

	assert [change.path for change in snapshot.changes] == ["old.txt"]
	assert snapshot.uncommitted

	assert (
		snapshot.base_sha
		== subprocess.check_output(
			["git", "rev-parse", "HEAD"], cwd=repo, text=True
		).strip()
	)


def test_snapshot_fingerprint_changes_with_file_contents(repo: Path) -> None:
	"""Keep the fingerprint until a changed file's contents change."""
	(repo / "old.txt").write_text("first edit\n")
	first = capture(repo)

	assert capture(repo).fingerprint == first.fingerprint

	(repo / "old.txt").write_text("second edit\n")
	second = capture(repo)

	assert second.fingerprint != first.fingerprint


def test_root_project_manifest_is_selected(repo: Path) -> None:
	"""Find a manifest and test configuration in a root-level project."""
	(repo / "pyproject.toml").write_text(
		"[project]\nname = 'example'\nversion = '0.1'\n"
	)
	(repo / "pytest.ini").write_text("[pytest]\n")
	(repo / "old.txt").write_text("changed\n")

	contents = select_files(repo, capture(repo), ())

	assert "pyproject.toml" in contents
	assert "pytest.ini" in contents


def test_includes_are_normalised(repo: Path) -> None:
	"""List a file once when its include uses a leading dot folder."""
	contents = select_files(repo, capture(repo), ("./README.md",))

	assert contents.count("README.md") == 1


def test_include_outside_repository_is_rejected(repo: Path) -> None:
	"""Reject an included path that leaves the repository."""
	with pytest.raises(MissingIncludeError, match="missing or outside the repository"):
		select_files(repo, capture(repo), ("../outside.txt",))


def test_absolute_include_inside_repository_is_rejected(repo: Path) -> None:
	"""Require included paths to be relative even when they resolve inside."""
	with pytest.raises(MissingIncludeError, match="missing or outside the repository"):
		select_files(repo, capture(repo), (str(repo / "README.md"),))


def test_non_list_settings_are_rejected(repo: Path) -> None:
	"""Reject a checks value that is not a list of strings."""
	(repo / ".review-packet.toml").write_text("checks = 1\n")

	with pytest.raises(InvalidSettingsError):
		read_settings(repo)


def test_unknown_settings_are_rejected(repo: Path) -> None:
	"""Report a misspelt settings key as invalid input."""
	(repo / ".review-packet.toml").write_text('includes = ["README.md"]\n')

	with pytest.raises(InvalidSettingsError, match="includes"):
		read_settings(repo)


def test_draft_text_and_usage_errors(
	repo: Path,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Print readable output and reject invalid user arguments with exit 2."""
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "drafts"))
	monkeypatch.chdir(repo)
	(repo / ".review-packet.toml").write_text('checks = ["test"]\n')

	assert main(["draft", "chk_example"]) == 0

	output = capsys.readouterr().out

	assert "Brief:" in output and "Settings: .review-packet.toml" in output

	brief = next((tmp_path / "drafts").rglob("REVIEW.md")).read_text()

	assert "Chunk: Draft" in brief
	assert "Draft a brief" in brief
	assert "Is it complete?" in brief
	assert "Settings: .review-packet.toml" in brief
	assert "### Callers\n\nNot listed yet." in brief
	assert "### Left out\n\nNot listed yet." in brief

	assert main(["draft", "chk_example", "--base", "missing", "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"

	assert main(["draft", "chk_example", "--include", "missing.txt", "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_null_progress_text_uses_review_prompt(
	repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Render missing progress text without passing null values into Markdown."""
	monkeypatch.setattr(
		records,
		"_read",
		lambda _root, *_args: {
			"id": "chk_null",
			"task_id": "tsk_null",
			"title": "Draft",
			"description": None,
			"review_question": None,
			"task": {
				"id": "tsk_null",
				"title": "Review",
				"overview": None,
				"done_when": [],
				"chunks": [],
			},
		},
	)

	chunk = read_chunk(repo, "chk_null")
	brief = render(chunk, capture(repo), (), None, RelatedTasks((), ()))

	assert chunk.description == ""
	assert chunk.task.overview == ""
	assert "## Main review question\n\nWrite this section." in brief


def test_snapshot_marks_staged_new_file_as_added(repo: Path) -> None:
	"""Keep a staged new file distinct from an untracked file."""
	(repo / "staged.txt").write_text("staged\n")
	(repo / "untracked.txt").write_text("untracked\n")
	git(repo, "add", "staged.txt")

	snapshot = capture(repo)

	assert {(change.path, change.status) for change in snapshot.changes} == {
		("staged.txt", "added"),
		("untracked.txt", "untracked"),
	}


def test_snapshot_before_first_commit_marks_index_as_added(tmp_path: Path) -> None:
	"""Show a staged file as added when the repository has no base commit."""
	git(tmp_path, "init", "-q")
	(tmp_path / "staged.txt").write_text("staged\n")
	git(tmp_path, "add", "staged.txt")

	snapshot = capture(tmp_path)
	chunk = Chunk(
		"chk_only",
		"Draft",
		"Draft a brief",
		"",
		Task("tsk_only", "Review", "Overview", (), ()),
	)
	brief = render(chunk, snapshot, (), None, RelatedTasks((), ()))

	assert snapshot.base_sha is None
	assert snapshot.changes[0].status == "added"
	assert f"Base commit: {NO_COMMITS}" in brief


def test_snapshot_detects_rename_with_edits(repo: Path) -> None:
	"""Keep the old path when a renamed file also changes."""
	(repo / "old.txt").write_text("same line\n" * 20)
	git(repo, "add", "old.txt")
	git(repo, "commit", "-qm", "expand file")
	git(repo, "mv", "old.txt", "renamed.txt")
	(repo / "renamed.txt").write_text("same line\n" * 19 + "different line\n")

	snapshot = capture(repo)

	assert ("renamed.txt", "renamed", "old.txt") in {
		(change.path, change.status, change.old_path) for change in snapshot.changes
	}


@pytest.mark.parametrize(
	("question", "main_question"),
	[
		("Team question? Outside review: Is this sound?", "Is this sound?"),
		("Is this complete?", "Is this complete?"),
	],
)
def test_brief_uses_outside_review_question_when_marked(
	repo: Path, question: str, main_question: str
) -> None:
	"""Keep the whole recorded question and focus the reviewer on its part."""
	chunk = Chunk(
		"chk_review",
		"Draft",
		"Draft a brief",
		question,
		Task(
			"tsk_review",
			"Review",
			"Overview",
			(),
			(
				TaskChunk("chk_review", "Draft", "active"),
				TaskChunk("chk_next", "Build", "pending"),
			),
		),
	)

	brief = render(chunk, capture(repo), (), None, RelatedTasks((), ()))

	assert f"Review question: {question}" in brief
	assert f"## Main review question\n\n{main_question}\n" in brief
	assert "- Build (pending)" in brief
	assert "## Recorded context" in brief


def test_brief_has_no_other_chunks_when_task_has_one(repo: Path) -> None:
	"""Show an explicit empty state for a task with one chunk."""
	chunk = Chunk(
		"chk_only",
		"Draft",
		"Draft a brief",
		"",
		Task("tsk_only", "Review", "Overview", (), ()),
	)

	brief = render(chunk, capture(repo), (), None, RelatedTasks((), ()))

	assert "### Other chunks\n\nNone." in brief


def test_brief_marks_agent_sections_and_empty_question(repo: Path) -> None:
	"""Mark each agent section and show "(none)" for a missing review question."""
	chunk = Chunk(
		"chk_only",
		"Draft",
		"Draft a brief",
		"",
		Task("tsk_only", "Review", "Overview", (), ()),
	)

	brief = render(chunk, capture(repo), (), None, RelatedTasks((), ()))

	assert "<!-- agent:start review-question -->" in brief
	assert "<!-- agent:end review-question -->" in brief
	assert "Review question: (none)" in brief


def test_argparse_usage_error_is_json(capsys: pytest.CaptureFixture[str]) -> None:
	"""Return a JSON usage envelope even when parsing fails."""
	with pytest.raises(SystemExit) as error:
		main(["draft", "--json"])

	assert error.value.code == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


@pytest.mark.parametrize(
	("code", "exit_code"), [("usage", 2), ("not-found", 1), ("environment", 3)]
)
def test_progress_error_keeps_its_kind(
	repo: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	code: str,
	exit_code: int,
) -> None:
	"""Map progress error kinds to the shared command-line exit codes."""

	def fail(_root: Path, _id: str) -> Chunk:
		"""Raise the requested progress failure through the draft command."""
		raise ProgressError("Progress failed.", code)

	monkeypatch.setattr(cli, "read_chunk", fail)
	monkeypatch.chdir(repo)

	assert main(["draft", "chk_example", "--json"]) == exit_code
	assert json.loads(capsys.readouterr().out)["error"]["code"] == code


def test_related_tasks_rejects_incomplete_export(
	repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Turn a malformed progress export into a command error."""
	monkeypatch.setattr(
		records, "_read", lambda _root, *_args: {"tasks": [{"title": "Missing ID"}]}
	)

	with pytest.raises(ProgressError, match="incomplete task details"):
		read_related_tasks(repo, "tsk_example")


def test_progress_error_without_object_keeps_fallback(
	repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Report an unusable progress error without a traceback."""
	monkeypatch.setattr(
		records.subprocess,
		"run",
		lambda *_args, **_kwargs: SimpleNamespace(
			stdout='{"ok": false, "error": "broken"}', returncode=1
		),
	)

	with pytest.raises(ProgressError, match="Progress could not read chunk get"):
		read_chunk(repo, "chk_example")
