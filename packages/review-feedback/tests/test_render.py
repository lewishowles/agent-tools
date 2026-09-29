import pytest

from review_feedback.draft import Draft, DraftEntry
from review_feedback.render import INTRO, render_packet


def make_entry(
	number: int,
	path: str,
	side: str,
	comment: str,
	selection_text: str = "",
) -> DraftEntry:
	"""Build a draft entry with stable coordinates for renderer tests."""
	return DraftEntry(
		number=number,
		path=path,
		side=side,
		start_line=2,
		start_column=3,
		end_line=4,
		end_column=8,
		selection_hash="selection-hash",
		repository_fingerprint="repository-fingerprint",
		comment=comment,
		selection_text=selection_text,
		old_path="old_name.py",
		new_path="new_name.py",
	)


def test_render_packet_uses_schema_for_current_and_removed_entries() -> None:
	draft = Draft(
		[
			make_entry(1, "new_name.py", "current", "Check this current code."),
			make_entry(3, "old_name.py", "removed", "Check this removed code."),
		]
	)

	packet = render_packet(draft)

	assert packet == INTRO + (
		"### 1. `new_name.py:2:3-4:8`\n\n"
		"Check this current code.\n\n"
		"### 3. `old_name.py:2:3-4:8` (removed at HEAD)\n\n"
		"Check this removed code.\n"
	)
	assert "# Review feedback" not in packet
	assert "> Check" not in packet


def test_render_packet_keeps_multiline_comments_plain() -> None:
	entry = make_entry(4, "review.py", "current", "First line\nSecond line")

	packet = render_packet(Draft([entry]))

	assert packet == INTRO + ("### 4. `review.py:2:3-4:8`\n\nFirst line\nSecond line\n")


def test_render_packet_includes_stored_selection_before_comment() -> None:
	entry = make_entry(
		1,
		"review.py",
		"current",
		"Check this code.",
		"def review():\n\treturn True",
	)

	packet = render_packet(Draft([entry]))

	assert packet == INTRO + (
		"### 1. `review.py:2:3-4:8`\n\n"
		"```py\n"
		"def review():\n"
		"\treturn True\n"
		"```\n\n"
		"Check this code.\n"
	)


@pytest.mark.parametrize(
	"line_break",
	["\n", "\r", "\r\n"],
	ids=["LF", "CR", "CRLF"],
)
def test_render_packet_preserves_trailing_selection_line_break(line_break: str) -> None:
	selection = f"def review():{line_break}\treturn True{line_break}"
	entry = make_entry(1, "review.py", "current", "Check this code.", selection)

	packet = render_packet(Draft([entry]))

	assert packet == INTRO + (
		f"### 1. `review.py:2:3-4:8`\n\n```py\n{selection}```\n\nCheck this code.\n"
	)


def test_render_packet_uses_a_longer_fence_for_embedded_backticks() -> None:
	selection = "```python\nvalue = 1\n```"
	entry = make_entry(2, "review.py", "removed", "Check the old code.", selection)

	packet = render_packet(Draft([entry]))

	assert packet == INTRO + (
		"### 2. `review.py:2:3-4:8` (removed at HEAD)\n\n"
		"````py\n"
		"```python\n"
		"value = 1\n"
		"```\n"
		"````\n\n"
		"Check the old code.\n"
	)


def test_render_packet_labels_extensionless_paths_as_text() -> None:
	entry = make_entry(5, "README", "current", "Check this text.", "Plain text")

	packet = render_packet(Draft([entry]))

	assert packet == INTRO + (
		"### 5. `README:2:3-4:8`\n\n```text\nPlain text\n```\n\nCheck this text.\n"
	)


def test_render_packet_lowercases_path_extension_label() -> None:
	entry = make_entry(6, "review.Py", "current", "Check this code.", "return True")

	packet = render_packet(Draft([entry]))

	assert packet == INTRO + (
		"### 6. `review.Py:2:3-4:8`\n\n```py\nreturn True\n```\n\nCheck this code.\n"
	)


def test_render_packet_returns_empty_body_for_empty_draft() -> None:
	assert render_packet(Draft([])) == ""
