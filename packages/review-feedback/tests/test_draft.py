from pathlib import Path
import subprocess

import pytest

from review_feedback.draft import (
	Draft,
	DraftValidationError,
	append_entry,
	remove_entry,
	repository_fingerprint,
	resolve_store,
	retire_active,
	save_active,
	validate_entries,
)
from review_feedback.git_selection import SelectionLocation


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


def test_stores_entries_under_git_metadata_without_worktree_state(
	tmp_path: Path,
) -> None:
	repo = make_repo(tmp_path)
	(repo / "review.txt").write_text("before\nnew selection\nafter\n", encoding="utf-8")
	status_before = git(repo, "status", "--porcelain")
	store = resolve_store(repo)
	draft = Draft([])
	location = SelectionLocation("review.txt", "current", 2, 1, 2, 14)
	append_entry(
		draft, location, "new selection", "Comment", repository_fingerprint(repo)
	)
	save_active(store, draft)

	assert store.active_path.exists()
	assert ".git" in store.active_path.parts
	assert "new selection" in store.active_path.read_text(encoding="utf-8")
	assert git(repo, "status", "--porcelain") == status_before


def test_removing_an_entry_keeps_remaining_numbers_stable(tmp_path: Path) -> None:
	repo = make_repo(tmp_path)
	draft = Draft([])
	location = SelectionLocation("review.txt", "current", 2, 1, 2, 14)
	fingerprint = repository_fingerprint(repo)
	append_entry(draft, location, "new selection", "First", fingerprint)
	append_entry(draft, location, "new selection", "Second", fingerprint)

	removed = remove_entry(draft, 1)

	assert removed.number == 1
	assert [entry.number for entry in draft.entries] == [2]
	assert draft.next_entry_number == 3


def test_validation_rejects_a_replaced_selection(tmp_path: Path) -> None:
	repo = make_repo(tmp_path)
	(repo / "review.txt").write_text("before\nnew selection\nafter\n", encoding="utf-8")
	store = resolve_store(repo)
	draft = Draft([])
	location = SelectionLocation("review.txt", "current", 2, 1, 2, 14)
	append_entry(
		draft, location, "new selection", "Comment", repository_fingerprint(repo)
	)

	(repo / "review.txt").write_text(
		"before\nreplaced selection\nafter\n", encoding="utf-8"
	)

	with pytest.raises(DraftValidationError, match="entry 1"):
		validate_entries(store, draft)


def test_validation_relocates_a_selection_after_lines_are_inserted(
	tmp_path: Path,
) -> None:
	repo = make_repo(tmp_path)
	(repo / "review.txt").write_text("before\nnew selection\nafter\n", encoding="utf-8")
	store = resolve_store(repo)
	draft = Draft([])
	append_entry(
		draft,
		SelectionLocation("review.txt", "current", 2, 1, 2, 14),
		"new selection",
		"Comment",
		repository_fingerprint(repo),
	)

	(repo / "review.txt").write_text(
		"inserted\nbefore\nnew selection\nafter\n", encoding="utf-8"
	)

	report = validate_entries(store, draft)

	assert not report.notices
	assert draft.entries[0].start_line == 3
	assert draft.entries[0].end_line == 3


def test_validation_reports_multiple_missing_entries_in_one_error(
	tmp_path: Path,
) -> None:
	repo = make_repo(tmp_path)
	(repo / "review.txt").write_text(
		"before\nfirst selection\nmiddle\nsecond selection\nafter\n",
		encoding="utf-8",
	)
	store = resolve_store(repo)
	draft = Draft([])
	append_entry(
		draft,
		SelectionLocation("review.txt", "current", 2, 1, 2, 16),
		"first selection",
		"First",
		repository_fingerprint(repo),
	)
	append_entry(
		draft,
		SelectionLocation("review.txt", "current", 4, 1, 4, 17),
		"second selection",
		"Second",
		repository_fingerprint(repo),
	)
	(repo / "review.txt").write_text(
		"before\nremoved first\nmiddle\nremoved second\nafter\n",
		encoding="utf-8",
	)

	with pytest.raises(DraftValidationError) as error:
		validate_entries(store, draft)

	assert error.value.entry_numbers == (1, 2)
	assert "entry 1" in str(error.value)
	assert "entry 2" in str(error.value)


def test_validation_reports_all_locations_for_an_ambiguous_relocation(
	tmp_path: Path,
) -> None:
	repo = make_repo(tmp_path)
	(repo / "review.txt").write_text("before\nnew selection\nafter\n", encoding="utf-8")
	store = resolve_store(repo)
	draft = Draft([])
	append_entry(
		draft,
		SelectionLocation("review.txt", "current", 2, 1, 2, 14),
		"new selection",
		"Comment",
		repository_fingerprint(repo),
	)
	(repo / "review.txt").write_text(
		"inserted\nbefore\nnew selection\nmiddle\nnew selection\nafter\n",
		encoding="utf-8",
	)

	report = validate_entries(store, draft)

	assert len(report.notices) == 1
	assert report.notices[0].candidate_locations == (
		"review.txt:3:1-3:14",
		"review.txt:5:1-5:14",
	)
	assert draft.entries[0].start_line == 2


def test_retiring_a_draft_moves_it_to_recoverable_task_trash(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	repo = make_repo(tmp_path)
	store = resolve_store(repo)
	save_active(store, Draft([]))
	trash_root = tmp_path / "Trash"
	monkeypatch.setattr("review_feedback.draft._trash_root", lambda: trash_root)

	retired_path = retire_active(store, "finished")

	assert retired_path.parent == trash_root
	assert retired_path.exists()
	assert not store.active_path.exists()
