import io
from pathlib import Path
import subprocess

import pytest

from review_feedback import cli
from review_feedback.draft import load_active, resolve_store


def git(repo: Path, *arguments: str) -> str:
	result = subprocess.run(
		["git", *arguments],
		cwd=repo,
		check=True,
		capture_output=True,
		text=True,
	)
	return result.stdout


def make_repo(tmp_path: Path) -> Path:
	repo = tmp_path / "repo"
	repo.mkdir()
	git(repo, "init", "--quiet")
	git(repo, "config", "user.email", "tests@example.com")
	git(repo, "config", "user.name", "Tests")
	(repo / "review.txt").write_text("before\nold selection\nafter\n", encoding="utf-8")
	git(repo, "add", ".")
	git(repo, "commit", "--quiet", "-m", "initial")
	return repo


def prepare_selection(repo: Path) -> None:
	(repo / "review.txt").write_text("before\nnew selection\nafter\n", encoding="utf-8")


def add_entry(
	repo: Path,
	monkeypatch: pytest.MonkeyPatch,
	comment: str = "Review this line",
	selection: str = "new selection",
) -> None:
	monkeypatch.chdir(repo)
	monkeypatch.setattr(cli, "read_clipboard", lambda: selection)
	monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
	monkeypatch.setattr(cli, "prompt", lambda _: comment)
	assert cli.main(["add"]) == 0


def test_commands_outside_a_worktree_fail_without_creating_state(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	monkeypatch.chdir(tmp_path)

	result = cli.main(["show"])

	assert result == 2
	assert "Git worktree" in capsys.readouterr().err
	assert list(tmp_path.iterdir()) == []


def test_add_creates_an_entry_for_each_matching_location(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	(repo / "review.txt").write_text(
		"before\nnew selection\nnew selection\nafter\n", encoding="utf-8"
	)
	monkeypatch.chdir(repo)
	monkeypatch.setattr(cli, "read_clipboard", lambda: "new selection")
	monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
	monkeypatch.setattr(cli, "prompt", lambda _: "Review both lines")

	assert cli.main(["add"]) == 0
	output = capsys.readouterr().out

	assert "entry 1 for review.txt:2:1-2:14" in output
	assert "entry 2 for review.txt:3:1-3:14" in output


def test_add_resolves_selection_before_prompting_for_comment(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	monkeypatch.chdir(repo)
	monkeypatch.setattr(cli, "read_clipboard", lambda: "missing selection")

	def fail_prompt(_: str) -> str:
		pytest.fail("comment should not be prompted for an invalid selection")

	monkeypatch.setattr(cli, "prompt", fail_prompt)

	assert cli.main(["add"]) == 2

	assert "selection was not found" in capsys.readouterr().err


def test_add_rejects_an_empty_comment_without_saving_a_draft(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	monkeypatch.chdir(repo)
	monkeypatch.setattr(cli, "read_clipboard", lambda: "new selection")
	monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
	monkeypatch.setattr(cli, "prompt", lambda _: "")

	assert cli.main(["add"]) == 2

	assert not resolve_store(repo).active_path.exists()
	assert "comment cannot be empty" in capsys.readouterr().err


def test_add_reports_an_eof_without_saving_a_draft(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	monkeypatch.chdir(repo)
	monkeypatch.setattr(cli, "read_clipboard", lambda: "new selection")
	monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

	def raise_eof(_: str) -> str:
		raise EOFError

	monkeypatch.setattr(cli, "prompt", raise_eof)

	assert cli.main(["add"]) == 2

	assert not resolve_store(repo).active_path.exists()
	assert "comment prompt closed" in capsys.readouterr().err


def test_add_preserves_ctrl_c_cancellation_without_saving_a_draft(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	monkeypatch.chdir(repo)
	monkeypatch.setattr(cli, "read_clipboard", lambda: "new selection")
	monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

	def raise_keyboard_interrupt(_: str) -> str:
		raise KeyboardInterrupt

	monkeypatch.setattr(cli, "prompt", raise_keyboard_interrupt)

	with pytest.raises(KeyboardInterrupt):
		cli.main(["add"])

	assert not resolve_store(repo).active_path.exists()


def test_add_accepts_a_comment_from_redirected_stdin(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	monkeypatch.chdir(repo)
	monkeypatch.setattr(cli, "read_clipboard", lambda: "new selection")
	monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Review from stdin\n"))

	assert cli.main(["add"]) == 0

	draft = load_active(resolve_store(repo))
	assert draft is not None
	assert draft.entries[0].comment == "Review from stdin"


def test_remove_and_show_report_success_in_plain_text(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch, "First review")
	add_entry(repo, monkeypatch, "Second review")
	monkeypatch.chdir(repo)
	capsys.readouterr()

	assert cli.main(["remove", "1"]) == 0
	remove_output = capsys.readouterr().out

	assert "Removed" in remove_output
	assert "entry 1" in remove_output

	assert cli.main(["show"]) == 0
	show_output = capsys.readouterr().out

	assert "Active draft" in show_output
	assert "Entry 2" in show_output
	assert "Second review" in show_output
	assert "Entry 1" not in show_output


def test_preview_copy_failure_leaves_the_active_draft_unchanged(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch)
	store = resolve_store(repo)
	before = store.active_path.read_bytes()
	monkeypatch.setattr(
		cli,
		"copy_to_clipboard",
		lambda text: (_ for _ in ()).throw(cli.ClipboardError("copy failed")),
	)
	monkeypatch.chdir(repo)

	assert cli.main(["preview", "--copy"]) == 2

	assert store.active_path.read_bytes() == before
	assert "copy failed" in capsys.readouterr().err


def test_show_relocates_an_entry_after_lines_are_inserted(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch)
	(repo / "review.txt").write_text(
		"inserted\nbefore\nnew selection\nafter\n", encoding="utf-8"
	)
	monkeypatch.chdir(repo)
	capsys.readouterr()

	assert cli.main(["show"]) == 0
	output = capsys.readouterr().out

	assert "review.txt:3:1-3:14" in output
	assert "review.txt:2:1-2:14" not in output


def test_show_reports_a_missing_entry_without_blocking(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch)
	(repo / "review.txt").write_text(
		"before\nreplaced selection\nafter\n", encoding="utf-8"
	)
	monkeypatch.chdir(repo)
	capsys.readouterr()

	assert cli.main(["show"]) == 0
	output = capsys.readouterr().out

	assert "Entry 1" in output
	assert "no longer contains the stored selection" in output


def test_preview_relocates_an_entry_and_writes_the_current_location(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch)
	(repo / "review.txt").write_text(
		"inserted\nbefore\nnew selection\nafter\n", encoding="utf-8"
	)
	monkeypatch.chdir(repo)
	capsys.readouterr()

	assert cli.main(["preview"]) == 0
	output = capsys.readouterr().out

	assert "### 1. `review.txt:3:1-3:14`" in output


def test_preview_reports_all_missing_entries_and_writes_no_packet(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	(repo / "review.txt").write_text(
		"before\nfirst selection\nmiddle\nsecond selection\nafter\n",
		encoding="utf-8",
	)
	add_entry(repo, monkeypatch, "First", "first selection")
	add_entry(repo, monkeypatch, "Second", "second selection")
	(repo / "review.txt").write_text(
		"before\nremoved first\nmiddle\nremoved second\nafter\n",
		encoding="utf-8",
	)
	monkeypatch.chdir(repo)
	capsys.readouterr()

	assert cli.main(["preview"]) == 2
	output = capsys.readouterr()

	assert "entry 1" in output.err
	assert "entry 2" in output.err
	assert "### 1." not in output.out


def test_clear_retires_an_abandoned_draft_without_a_packet(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch)
	trash_root = tmp_path / "Trash"
	monkeypatch.setattr("review_feedback.draft._trash_root", lambda: trash_root)
	monkeypatch.chdir(repo)
	capsys.readouterr()

	assert cli.main(["clear"]) == 0

	assert not resolve_store(repo).active_path.exists()
	assert list(trash_root.glob("*.json"))
	assert "Review feedback" not in capsys.readouterr().out


def test_stale_preview_preserves_the_draft_and_identifies_the_entry(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch)
	store = resolve_store(repo)
	before = store.active_path.read_bytes()
	(repo / "review.txt").write_text(
		"before\nreplaced selection\nafter\n", encoding="utf-8"
	)
	monkeypatch.chdir(repo)

	assert cli.main(["preview"]) == 2

	assert store.active_path.read_bytes() == before
	assert "entry 1" in capsys.readouterr().err


def test_finish_retires_the_draft_and_next_add_starts_at_one(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	repo = make_repo(tmp_path)
	prepare_selection(repo)
	add_entry(repo, monkeypatch)
	(repo / "review.txt").write_text(
		"inserted\nbefore\nnew selection\nafter\n", encoding="utf-8"
	)
	monkeypatch.setattr(cli, "copy_to_clipboard", lambda text: None)
	trash_root = tmp_path / "Trash"
	monkeypatch.setattr("review_feedback.draft._trash_root", lambda: trash_root)
	monkeypatch.chdir(repo)

	assert cli.main(["finish", "--copy"]) == 0
	finish_output = capsys.readouterr().out
	store = resolve_store(repo)

	assert "### 1. `review.txt:3:1-3:14`" in finish_output
	assert "Review this line" in finish_output
	assert not store.active_path.exists()
	assert list(trash_root.glob("*.json"))

	add_entry(repo, monkeypatch, "New review")
	assert store.active_path.read_text(encoding="utf-8").count('"number": 1') == 1
