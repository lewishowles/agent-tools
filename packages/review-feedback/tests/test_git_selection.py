from pathlib import Path
import subprocess

import pytest

from review_feedback.git_selection import (
	EmptySelectionError,
	GitWorktreeError,
	SelectionError,
	SelectionLocation,
	SelectionNotFoundError,
	SelectionSpansSidesError,
	resolve_selection,
)


def git(repo: Path, *arguments: str) -> None:
	subprocess.run(
		["git", *arguments],
		cwd=repo,
		check=True,
		capture_output=True,
	)


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
	repo = tmp_path / "repo"
	repo.mkdir()
	git(repo, "init", "--quiet")
	git(repo, "config", "user.email", "tests@example.com")
	git(repo, "config", "user.name", "Tests")
	for name, content in files.items():
		path = repo / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")
	git(repo, "add", ".")
	git(repo, "commit", "--quiet", "-m", "initial")
	return repo


def location(
	path: str,
	side: str,
	start_line: int,
	start_column: int,
	end_line: int,
	end_column: int,
	old_path: str | None = None,
	new_path: str | None = None,
) -> SelectionLocation:
	return SelectionLocation(
		path,
		side,
		start_line,
		start_column,
		end_line,
		end_column,
		old_path,
		new_path,
	)


def test_resolves_a_mid_line_single_line_selection(tmp_path: Path) -> None:
	repo = make_repo(tmp_path, {"review.txt": "unchanged\nold\n"})
	text = 'implementer "codex|implementer|gpt-5.6-luna|implementer.md|xhigh"'
	(repo / "review.txt").write_text(f"prefix {text} suffix\n", encoding="utf-8")

	result = resolve_selection(
		"codex|implementer|gpt-5.6-luna|implementer.md|xhigh", repo
	)

	assert result == [location("review.txt", "current", 1, 21, 1, 72)]


def test_resolves_a_partial_multi_line_selection(tmp_path: Path) -> None:
	repo = make_repo(tmp_path, {"review.txt": "before\nold\nafter\n"})
	(repo / "review.txt").write_text(
		"before\nalpha first\nmiddle line\nlast line\nafter\n",
		encoding="utf-8",
	)

	result = resolve_selection("first\nmiddle line\nlast", repo)

	assert result == [location("review.txt", "current", 2, 7, 4, 5)]


def test_resolves_unchanged_context_on_the_current_side(tmp_path: Path) -> None:
	repo = make_repo(tmp_path, {"review.txt": "before\ncontext stable\nold\n"})
	(repo / "review.txt").write_text(
		"before\ncontext stable\nnew\n",
		encoding="utf-8",
	)

	result = resolve_selection("context stable", repo)

	assert result == [location("review.txt", "current", 2, 1, 2, 15)]


def test_resolves_added_removed_renamed_and_untracked_locations(
	tmp_path: Path,
) -> None:
	repo = make_repo(
		tmp_path,
		{
			"added.txt": "base\n",
			"removed.txt": "keep\nremoved phrase\n",
			"old-name.txt": (
				"context alpha\ncontext beta\nold rename phrase\n"
				"context gamma\ncontext delta\n"
			),
		},
	)
	(repo / "added.txt").write_text("base\nadded phrase\n", encoding="utf-8")
	(repo / "removed.txt").write_text("keep\n", encoding="utf-8")
	git(repo, "mv", "old-name.txt", "new-name.txt")
	(repo / "new-name.txt").write_text(
		"context alpha\ncontext beta\nnew rename phrase\n"
		"context gamma\ncontext delta\n",
		encoding="utf-8",
	)
	(repo.joinpath("untracked.txt")).write_text("untracked phrase\n", encoding="utf-8")

	assert resolve_selection("added phrase", repo) == [
		location("added.txt", "current", 2, 1, 2, 13)
	]
	assert resolve_selection("removed phrase", repo) == [
		location("removed.txt", "removed", 2, 1, 2, 15)
	]
	assert resolve_selection("old rename phrase", repo) == [
		location(
			"old-name.txt",
			"removed",
			3,
			1,
			3,
			18,
			"old-name.txt",
			"new-name.txt",
		)
	]
	assert resolve_selection("new rename phrase", repo) == [
		location(
			"new-name.txt",
			"current",
			3,
			1,
			3,
			18,
			"old-name.txt",
			"new-name.txt",
		)
	]
	assert resolve_selection("untracked phrase", repo) == [
		location("untracked.txt", "current", 1, 1, 1, 17)
	]


def test_rejects_undecodable_binary_content(tmp_path: Path) -> None:
	repo = make_repo(tmp_path, {"review.txt": "content\n"})
	(repo / "binary.dat").write_bytes(b"binary selection \xfe\n")

	with pytest.raises(SelectionNotFoundError):
		resolve_selection("binary selection", repo)


def test_resolves_every_repeated_text_location(tmp_path: Path) -> None:
	repo = make_repo(tmp_path, {"review.txt": "first\nrepeat\nrepeat\n"})
	(repo / "review.txt").write_text(
		"first\nrepeat\nrepeat changed\n", encoding="utf-8"
	)

	result = resolve_selection("repeat", repo)

	assert result == [
		location("review.txt", "current", 2, 1, 2, 7),
		location("review.txt", "current", 3, 1, 3, 7),
	]


def test_rejects_a_selection_that_combines_old_and_current_sides(
	tmp_path: Path,
) -> None:
	repo = make_repo(tmp_path, {"review.txt": "old line\nkeep\n"})
	(repo / "review.txt").write_text("new line\nkeep\n", encoding="utf-8")

	with pytest.raises(SelectionSpansSidesError, match="diff side"):
		resolve_selection("old line\nnew line", repo)


@pytest.mark.parametrize(
	"selection, error",
	[
		("", EmptySelectionError),
		("absent", SelectionNotFoundError),
	],
)
def test_rejects_empty_and_absent_selections(
	tmp_path: Path, selection: str, error: type[SelectionError]
) -> None:
	repo = make_repo(tmp_path, {"review.txt": "content\n"})

	with pytest.raises(error):
		resolve_selection(selection, repo)


def test_rejects_a_directory_outside_a_git_worktree(tmp_path: Path) -> None:
	with pytest.raises(GitWorktreeError, match="Git worktree"):
		resolve_selection("selection", tmp_path)
