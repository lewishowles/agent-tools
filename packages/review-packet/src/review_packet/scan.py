"""Find credentials and local paths before files leave a repository."""

import re
from dataclasses import dataclass
from pathlib import Path

from review_packet.selection import OutsideSymlinkError, leads_outside

# A macOS, Linux or Windows home folder path, such as a user folder under
# /Users. It must not follow a letter, digit, underscore or dot, so a web link
# with /home/ in it passes. The pattern is written so this file does not match itself.
HOME_PATH_PATTERN = re.compile(
	r"""(?<![\w.])(?:/(?:Users|home)/|[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/))[^/\\\s"']+(?=[/\\"'\s]|$)""",
	re.IGNORECASE,
)

# Recognisable credentials and home folder paths, each with the kind shown to
# the user.
PATTERNS = (
	(
		"private key",
		re.compile(
			r"-----BEGIN (?:(?:[A-Z0-9 ]+ )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
		),
	),
	("AWS key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
	(
		"GitHub token",
		re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{20,})\b"),
	),
	("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
	("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b")),
	("sk key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
	("home path", HOME_PATH_PATTERN),
)

# Environment templates may be shared, but their contents are still scanned.
ENV_EXAMPLES = frozenset({".env.example", ".env.sample"})


@dataclass(frozen=True)
class Finding:
	"""A place where the scan found material that can't be shared.

	The matched text is never kept, so a finding is safe to print.
	"""

	# The repository-relative path, or the name of a generated document.
	path: str
	# The source line, or 1 for a file-name finding.
	line: int
	# The kind of material that cannot be shared.
	kind: str


def scan_files(root: Path, paths: tuple[str, ...]) -> tuple[Finding, ...]:
	"""Find material in selected repository files that cannot be shared.

	Args:
		root: The repository root containing the selected files.
		paths: Repository-relative paths selected for the packet.

	Returns:
		Findings with locations and kinds, without matched values.

	Raises:
		OutsideSymlinkError: A selected path leads outside the repository.
		OSError: A selected file cannot be read.
	"""
	# Findings in selection order.
	findings = []

	for path in paths:
		if leads_outside(root, path):
			raise OutsideSymlinkError(
				f"Path that leads outside the repository through a link can't be shared: {path}"
			)

		findings.extend(scan_text(path, (root / path).read_text(errors="replace")))

	return tuple(findings)


def scan_content(contents: dict[str, bytes]) -> tuple[Finding, ...]:
	"""Scan the exact selected bytes that build will place in the archive.

	Args:
		contents: Repository-relative names and their already-read file bytes.

	Returns:
		Findings with locations and kinds, without matched values.
	"""
	return tuple(
		finding
		for path, content in contents.items()
		for finding in scan_text(path, content.decode(errors="replace"))
	)


def scan_text(path: str, content: str) -> tuple[Finding, ...]:
	"""Find blocked file names and content in a packet item.

	Args:
		path: The repository-relative path or generated document name.
		content: The text that would be shared.

	Returns:
		Each matching location and kind, without its matched value.
	"""
	# Findings for this file or generated document.
	findings = []
	# The file name, lowercased so .ENV is refused too.
	name = Path(path).name.lower()

	if (name == ".env" or name.startswith(".env.")) and name not in ENV_EXAMPLES:
		findings.append(Finding(path, 1, "env file"))

	for line_number, line in enumerate(content.splitlines(), 1):
		for kind, pattern in PATTERNS:
			if pattern.search(line):
				findings.append(Finding(path, line_number, kind))

	return tuple(findings)
