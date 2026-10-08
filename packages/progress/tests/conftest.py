"""Shared Git repositories for progress tests."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def committed_repository(tmp_path: Path) -> Path:
	"""Provide a repository with one committed file for linked worktree tests."""
	repository = tmp_path / "repository"
	repository.mkdir()
	subprocess.run(["git", "init", "--quiet", str(repository)], check=True)

	(repository / "tracked.txt").write_text("committed content\n")
	subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)

	subprocess.run(
		[
			"git",
			"-C",
			str(repository),
			"-c",
			"user.name=Test",
			"-c",
			"user.email=test@example.com",
			"commit",
			"--quiet",
			"-m",
			"Initial commit",
		],
		check=True,
	)

	return repository
