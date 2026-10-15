"""Tests for grouping tool failures by command family and error class."""

import pytest

from friction.tool_errors import classify_tool_error


@pytest.mark.parametrize(
	("command", "expected"),
	[
		("git diff --stat", "git"),
		("/usr/bin/git status", "git"),
		("cd /repo && git diff", "git"),
		("cd '/my repo'; cd src && git status", "git"),
		("FLAG=1 cd /repo && git status", "git"),
		("FLAG=1 git status", "git"),
		("env FLAG=1 git status", "git"),
		("timeout 30 git status", "git"),
		("uv run pytest packages/friction", "pytest"),
		("npx eslint src", "eslint"),
		("cd repo && env FLAG=1 timeout 30 uv run npx eslint src", "eslint"),
		('git commit -m "unfinished', "git"),
		("cd /repo", "cd"),
	],
)
def test_bash_command_family(command: str, expected: str) -> None:
	"""A Bash command's family is the program name after cd steps, assignments and wrappers."""
	assert classify_tool_error("Bash", command, "failed") == (expected, "failed")


def test_other_tool_uses_its_name() -> None:
	"""A non-Bash tool keeps its own name as the command family."""
	assert classify_tool_error("Read", "/tmp/file.txt", "failed") == (
		"Read",
		"failed",
	)


@pytest.mark.parametrize(
	("error", "expected"),
	[
		("Exit code 127: command missing", "exit 127"),
		(" Exit code 1: command failed", "exit 1"),
		(
			"Could not open /repo/one/file.py at line 12. More detail",
			"Could not open PATH at line N.",
		),
		(
			"Could not open /repo/two/file.py at line 83. Different detail",
			"Could not open PATH at line N.",
		),
		(
			"String to replace not found in file. String: first old text",
			"String to replace not found in file.",
		),
		(
			"String to replace not found in file. String: another old text",
			"String to replace not found in file.",
		),
		("Failed after 12 attempts", "Failed after N attempts"),
	],
)
def test_error_class(error: str, expected: str) -> None:
	"""Exits and errors that differ only by paths or numbers share a class."""
	assert classify_tool_error("Bash", "git status", error) == ("git", expected)


@pytest.mark.parametrize(
	("tool_name", "command", "error", "expected"),
	[
		(None, "git status", "failed", ("unknown", "failed")),
		("", "git status", "failed", ("unknown", "failed")),
		("  ", "git status", "failed", ("unknown", "failed")),
		("Bash", None, "failed", ("unknown", "failed")),
		("Bash", "", "failed", ("unknown", "failed")),
		("Bash", "  ", "failed", ("unknown", "failed")),
		("Bash", "unknown input", "failed", ("unknown", "failed")),
		("Bash", "git status", None, ("git", "unknown")),
		("Bash", "git status", "", ("git", "unknown")),
		("Bash", "git status", "  ", ("git", "unknown")),
		("Bash", "git status", "unknown error", ("git", "unknown")),
		(None, None, None, ("unknown", "unknown")),
	],
)
def test_missing_fields(
	tool_name: str | None,
	command: str | None,
	error: str | None,
	expected: tuple[str, str],
) -> None:
	"""Missing stored fields and the hook's fallback text group under unknown."""
	assert classify_tool_error(tool_name, command, error) == expected
