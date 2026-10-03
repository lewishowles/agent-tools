"""Check heading discovery through the public command."""

import json
from pathlib import Path

import pytest
from page_to_markdown.cli import main
from page_to_markdown.convert import convert_to_markdown
from page_to_markdown.outline import HeadingLookupError, extract_section, list_headings


# Local HTML used by every heading command test.
FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
	"heading",
	[
		"<h2>First<br>Second</h2>",
		"<h2>First <br><br> Second</h2>",
		"<h2><br>First<br>Second<br></h2>",
	],
)
def test_heading_breaks_keep_full_text_on_one_line(heading) -> None:
	"""A heading with line breaks converts to one line, with each break as one space and breaks at the edges dropped."""
	assert convert_to_markdown(heading) == "## First Second\n"


def test_heading_commands_use_full_text_across_line_break(tmp_path, capsys) -> None:
	"""Listing headings and selecting a section both use the whole heading when it contains a line break."""
	source = tmp_path / "heading-break.html"
	source.write_text("<h2>First<br>Second</h2><p>Body</p>", encoding="utf-8")

	list_exit_code = main([str(source), "--list-headings"])
	listed = capsys.readouterr()
	section_exit_code = main([str(source), "--heading", "First Second"])
	section = capsys.readouterr()

	assert list_exit_code == 0
	assert listed.out == "2  First Second  #first-second\n"
	assert section_exit_code == 0
	assert section.out == "## First Second\n\nBody\n"


def test_list_headings_reports_plain_text_and_unique_selectors(capsys) -> None:
	"""The outline includes real headings in document order with stable anchors."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--list-headings"])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.out.splitlines() == [
		"1  Guide  #guide",
		"2  Install the tool  #install-the-tool",
		"3  Quick start  #quick-start",
		"2  Install the tool  #install-the-tool-1",
		"2  Reference and examples  #reference-and-examples",
		"2  Reference & examples  #reference--examples",
		"2  5 * 3  #5--3",
	]


def test_list_headings_json_has_source_and_heading_fields(capsys) -> None:
	"""JSON keeps the source and the same ordered outline as text output."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--list-headings", "--json"])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert json.loads(captured.out) == {
		"source": source,
		"headings": [
			{"level": 1, "text": "Guide", "selector": "guide"},
			{"level": 2, "text": "Install the tool", "selector": "install-the-tool"},
			{"level": 3, "text": "Quick start", "selector": "quick-start"},
			{"level": 2, "text": "Install the tool", "selector": "install-the-tool-1"},
			{
				"level": 2,
				"text": "Reference and examples",
				"selector": "reference-and-examples",
			},
			{
				"level": 2,
				"text": "Reference & examples",
				"selector": "reference--examples",
			},
			{"level": 2, "text": "5 * 3", "selector": "5--3"},
		],
	}


@pytest.mark.parametrize(
	("source", "expected"),
	[
		("5 * 3 * 4", "5 * 3 * 4"),
		("**a** b *c*", "a b c"),
		("*a* *b*", "a b"),
		("***a***", "a"),
		("*a **b** c*", "a b c"),
		("a ** b ** c", "a ** b ** c"),
	],
)
def test_list_headings_removes_only_paired_emphasis(source, expected) -> None:
	"""Heading text removes paired emphasis without changing literal asterisks."""
	assert list_headings(f"## {source}")[0].text == expected


def test_list_headings_keeps_asterisks_inside_code_spans() -> None:
	"""Code span asterisks stay visible while neighbouring emphasis is removed."""
	heading = list_headings("## Use `*args*` with **options**")[0]

	assert heading.text == "Use *args* with options"
	assert heading.selector == "use-args-with-options"


def test_heading_selects_one_duplicate_by_selector(capsys) -> None:
	"""A numbered selector picks the second of two headings with the same text."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", "install-the-tool-1"])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.out.startswith("## Install `the tool`\n")
	assert "## Reference" not in captured.out
	assert "### Quick" not in captured.out


def test_heading_accepts_selector_with_leading_hash(capsys) -> None:
	"""A selector copied from the outline can include its leading hash."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", "#install-the-tool-1"])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.out.startswith("## Install `the tool`\n")
	assert "## Reference" not in captured.out


def test_heading_matches_formatted_text_and_includes_children(capsys) -> None:
	"""Text queries ignore case, extra spaces, and inline Markdown syntax."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", "**guide**", "--json"])
	captured = capsys.readouterr()
	result = json.loads(captured.out)

	assert exit_code == 0
	assert result["source"] == source
	assert result["heading"] == {"level": 1, "text": "Guide", "selector": "guide"}
	assert result["markdown"].startswith("# Guide\n")
	assert "### Quick **start**" in result["markdown"]
	assert "## Reference" in result["markdown"]


def test_heading_without_children_stops_at_first_child(capsys) -> None:
	"""The shorter section ends before the next heading at any level."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", "guide", "--without-children"])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.out.startswith("# Guide\n")
	assert "## Install" not in captured.out


def test_heading_text_query_reports_duplicate_candidates(capsys) -> None:
	"""Duplicate heading text needs an anchor selector to pick one section."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", "Install   `the tool`", "--json"])
	captured = capsys.readouterr()

	assert exit_code == 1
	assert json.loads(captured.out) == {
		"error": {
			"kind": "ambiguous",
			"query": "Install   `the tool`",
			"candidates": [
				{
					"level": 2,
					"text": "Install the tool",
					"selector": "install-the-tool",
				},
				{
					"level": 2,
					"text": "Install the tool",
					"selector": "install-the-tool-1",
				},
			],
		}
	}


def test_ambiguous_heading_text_reports_selectors_on_stderr(capsys) -> None:
	"""Text mode shows each selector when a heading name repeats."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", "Install the tool"])
	captured = capsys.readouterr()

	assert exit_code == 1
	assert captured.out == ""
	assert "Several headings match" in captured.err
	assert "#install-the-tool\n" in captured.err
	assert "#install-the-tool-1" in captured.err


def test_missing_heading_candidates_follow_closeness_order() -> None:
	"""Suggestions rank closer heading names before earlier document headings."""
	markdown = "# Installation\n# Install\n"

	with pytest.raises(HeadingLookupError) as error:
		extract_section(markdown, "Instal")

	assert [heading.text for heading in error.value.candidates] == [
		"Install",
		"Installation",
	]


@pytest.mark.parametrize(
	("query", "expected"),
	[
		("Quick stat", "#quick-start"),
		("completely unrelated", "--list-headings"),
		("Not a heading", "--list-headings"),
	],
)
def test_missing_heading_gives_candidates_or_outline_hint(
	query, expected, capsys
) -> None:
	"""Absent headings fail with a close selector or a way to inspect the outline."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", query])
	captured = capsys.readouterr()

	assert exit_code == 1
	assert captured.out == ""
	assert expected in captured.err


def test_extract_section_ignores_fenced_headings_as_boundaries() -> None:
	"""A heading printed in a code fence stays inside its real parent section."""
	markdown = "# Start\n```\n# Not a heading\n```\n## Child\ntext\n# Next\n"

	heading, section = extract_section(markdown, "start")

	assert heading.selector == "start"
	assert section == "# Start\n```\n# Not a heading\n```\n## Child\ntext\n"


def test_missing_heading_json_has_no_candidates_when_nothing_is_close(capsys) -> None:
	"""The JSON failure still reports the query when no heading is close enough to suggest."""
	source = str(FIXTURES / "headings.html")

	exit_code = main([source, "--heading", "completely unrelated", "--json"])
	captured = capsys.readouterr()

	assert exit_code == 1
	assert json.loads(captured.out) == {
		"error": {
			"kind": "missing",
			"query": "completely unrelated",
			"candidates": [],
		}
	}


@pytest.mark.parametrize("non_closing_fence", ["```", "~~~~"])
def test_list_headings_ignores_heading_after_non_closing_fence(
	non_closing_fence,
) -> None:
	"""Shorter and differently marked fences leave the code block open."""
	markdown = f"````\n# Hidden\n{non_closing_fence}\n# Still hidden\n````\n# Visible"

	assert [
		(heading.text, heading.selector) for heading in list_headings(markdown)
	] == [("Visible", "visible")]


@pytest.mark.parametrize(
	"arguments",
	[
		["--json"],
		["--list-headings", "--json", "--confidence"],
		["--list-headings", "--json", "--copy"],
		["--list-headings", str(FIXTURES / "simple.html")],
		["--heading", "Guide", "--list-headings"],
		["--heading", "Guide", str(FIXTURES / "simple.html")],
		["--without-children"],
		["--list-headings", "--without-children"],
	],
)
def test_heading_usage_errors_exit_two(arguments, capsys) -> None:
	"""Invalid heading command combinations fail before any source is read."""
	with pytest.raises(SystemExit) as error:
		main([str(FIXTURES / "headings.html"), *arguments])

	captured = capsys.readouterr()

	assert error.value.code == 2
	assert captured.out == ""
	assert "error:" in captured.err
