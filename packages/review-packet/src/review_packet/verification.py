"""Write the author's check results for an outside reviewer."""

import re
from pathlib import Path

from review_packet.checks import CheckResult

# The opening of every report, saying whose results these are.
HEADER = (
	"# Verification",
	"",
	"These are the author's results from the snapshot recorded in REVIEW.md. "
	"The reviewer has not re-run these checks.",
	"",
)


def render(root: Path, results: tuple[CheckResult, ...]) -> str:
	"""Return the Markdown report of check results, without the repository's path.

	Args:
		root: The repository root, written as a relative path in the report.
		results: The configured checks and their results.

	Returns:
		The verification report, which says so when no checks were configured.
	"""
	# The report lines, starting with the header.
	lines = list(HEADER)

	if not results:
		lines.extend(("No checks were configured.", ""))
		return "\n".join(lines)

	for result in results:
		lines.extend(_result_lines(root, result))

	return "\n".join(lines)


def _result_lines(root: Path, result: CheckResult) -> list[str]:
	"""Return the report section for one check.

	Args:
		root: The repository root, written as a relative path.
		result: The check and its result.

	Returns:
		The heading, command, state, run time and any first failure output.
	"""
	# A check that did not run has no run time.
	run_time = (
		"not run"
		if result.duration_seconds is None
		else f"{result.duration_seconds:.3f}s"
	)
	# The section, ending with a blank line.
	lines = [
		f"## {result.name}",
		"",
		f"- Command: `{_relative(root, result.command)}`",
		f"- State: {result.state}",
		f"- Run time: {run_time}",
	]

	if result.failure:
		lines.extend(
			(
				"- First failure output:",
				"",
				"```text",
				_relative(root, result.failure),
				"```",
			)
		)

	lines.append("")
	return lines


def _relative(root: Path, value: str) -> str:
	"""Rewrite paths inside the repository as paths relative to its root.

	Args:
		root: The repository root.
		value: A command or failure output from agent-run.

	Returns:
		The text with paths inside the repository made relative, and the
		repository root itself written as ".".
	"""
	# Paths inside the repository lose their root prefix, and the root on its own becomes a dot.
	# The root only counts when it is not part of a longer path, such as a sibling folder
	# or a path that ends with the root's path.
	root_pattern = rf"(?<![\w./-]){re.escape(str(root))}"
	relative = re.sub(rf"{root_pattern}/", "", value)
	return re.sub(rf"{root_pattern}(?=$|[^\w./-])", ".", relative)
