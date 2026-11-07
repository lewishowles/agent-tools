"""Check that the packet scan reports locations without secret values."""

from pathlib import Path

import pytest

from review_packet.scan import Finding, scan_files, scan_text
from review_packet.selection import OutsideSymlinkError


@pytest.mark.parametrize(
	("content", "kind"),
	[
		("-----BEGIN RSA " + "PRIVATE KEY-----", "private key"),
		("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----", "private key"),
		("AKIA" + "1234567890123456", "AWS key"),
		("ghp_" + "a" * 36, "GitHub token"),
		("github_pat_" + "a" * 20, "GitHub token"),
		("xoxb" + "-1234567890-1234567890", "Slack token"),
		("npm_" + "a" * 36, "npm token"),
		("sk-" + "a" * 24, "sk key"),
		("/" + "Users/example/Documents/", "home path"),
		("/" + "home/example/Documents/", "home path"),
		("C:\\" + "Users\\example\\Documents\\", "home path"),
	],
)
def test_scan_text_reports_only_location_and_kind(content: str, kind: str) -> None:
	"""Keep matched text out of a finding for each supported pattern."""
	# The report for a matched line.
	findings = scan_text("notes.txt", f"safe\n{content}\n")

	assert findings == (Finding("notes.txt", 2, kind),)
	assert content not in repr(findings)


@pytest.mark.parametrize(
	"content",
	[
		"/" + "Users/example",
		"cd /" + "home/example && ls",
		'HOME="/' + 'Users/example"',
		"c:\\" + "users\\example",
		"C:\\\\" + "Users\\\\example",
		"file:///" + "Users/example/notes.txt",
	],
	ids=[
		"mac path ends at line end",
		"linux path ends at whitespace",
		"path ends at quote",
		"lowercase windows path",
		"json escaped windows path",
		"file URL contains a local home path",
	],
)
def test_scan_text_finds_home_folder_boundaries(content: str) -> None:
	"""Find a home folder regardless of the following text or slash form."""
	assert scan_text("notes.txt", content) == (Finding("notes.txt", 1, "home path"),)


def test_scan_text_allows_home_path_inside_url() -> None:
	"""Allow a web URL whose path happens to contain a home folder name."""
	content = "https://example.com/" + "home/docs/intro"

	assert scan_text("notes.txt", content) == ()


@pytest.mark.parametrize("name", [".env", ".env.local", ".ENV.production"])
def test_scan_text_refuses_env_files(name: str) -> None:
	"""Reject environment files even when their contents are empty."""
	assert scan_text(f"config/{name}", "") == (
		Finding(f"config/{name}", 1, "env file"),
	)


@pytest.mark.parametrize("name", [".env.example", ".env.sample"])
def test_scan_text_allows_env_templates(name: str) -> None:
	"""Allow only the two named environment templates."""
	assert scan_text(name, "PLACEHOLDER=change-me\n") == ()


def test_scan_files_refuses_outside_link(tmp_path: Path) -> None:
	"""Never read a selected file through a link outside the repository."""
	# The selected link and its outside target.
	root = tmp_path / "repo"
	root.mkdir()
	outside = tmp_path / "outside.txt"
	outside.write_text("safe\n")
	(root / "linked.txt").symlink_to(outside)

	with pytest.raises(OutsideSymlinkError):
		scan_files(root, ("linked.txt",))
