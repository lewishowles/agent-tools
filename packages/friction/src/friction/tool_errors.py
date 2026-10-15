"""Classify recorded tool failures for summary grouping."""

import re
import shlex
from pathlib import PurePath

# A `NAME=value` environment assignment before a command, which is skipped when
# finding the program name.
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

# The `Exit code N` text at the start of a failed shell command's error.
EXIT_CODE = re.compile(r"^Exit code (\d+)\b")

# A run of digits, replaced with N so errors that differ only in counts or line
# numbers share a class.
NUMBER = re.compile(r"\d+")

# A word containing a `/`, replaced with PATH so errors that differ only in file
# paths share a class.
PATH = re.compile(r"\S*/\S*")

# Recorded commands that mean no command was captured, including the
# "unknown input" fallback the tool-failure hook records.
MISSING_INPUTS = frozenset({"", "unknown input"})

# Recorded errors that mean no error text was captured, including the
# "unknown error" fallback the tool-failure hook records.
MISSING_ERRORS = frozenset({"", "unknown error"})


def _command_family(command: str) -> str:
	"""Return the program a shell command runs, or unknown when there is none.

	Leading cd steps, VAR=value assignments and the env, timeout N, uv run and
	npx wrappers are skipped. A command that is only cd steps returns cd.
	"""
	try:
		lexer = shlex.shlex(command, posix=True, punctuation_chars=";&")
		lexer.whitespace_split = True
		tokens = list(lexer)
	except ValueError:
		# Hook values can be truncated in the middle of a quoted argument.
		tokens = command.split()

	while tokens:
		if tokens[0] == "cd":
			separator = next(
				(index for index, token in enumerate(tokens) if token in {"&&", ";"}),
				None,
			)
			if separator is None:
				return "cd"
			tokens = tokens[separator + 1 :]
		elif ASSIGNMENT.fullmatch(tokens[0]):
			tokens.pop(0)
		elif tokens[0] == "env":
			tokens.pop(0)
		elif tokens[0] == "timeout" and len(tokens) > 1:
			tokens = tokens[2:]
		elif tokens[:2] == ["uv", "run"]:
			tokens = tokens[2:]
		elif tokens[0] == "npx":
			tokens.pop(0)
		else:
			return PurePath(tokens[0]).name or "unknown"

	return "unknown"


def _error_class(error: str | None) -> str:
	"""Return exit N for a failed shell command, otherwise the error's first sentence with numbers and paths replaced.

	A missing error returns unknown.
	"""
	if error is None:
		return "unknown"

	error = error.strip()
	if error in MISSING_ERRORS:
		return "unknown"

	exit_code = EXIT_CODE.match(error)
	if exit_code:
		return f"exit {exit_code.group(1)}"

	sentence_end = error.find(". ")
	first_sentence = error[: sentence_end + 1] if sentence_end >= 0 else error
	return PATH.sub("PATH", NUMBER.sub("N", first_sentence))


def classify_tool_error(
	tool_name: str | None,
	command: str | None,
	error: str | None,
) -> tuple[str, str]:
	"""Return the command family and error class that friction summary groups a tool failure by.

	Only Bash commands are read for a program name; other tools use their own
	name. Any value may be missing, and a missing value gives unknown.
	"""
	if not tool_name or not tool_name.strip():
		family = "unknown"
	elif tool_name == "Bash":
		family = (
			_command_family(command)
			if command is not None and command.strip() not in MISSING_INPUTS
			else "unknown"
		)
	else:
		family = tool_name

	return family, _error_class(error)
