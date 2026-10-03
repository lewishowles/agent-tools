"""Check heading discovery through the public command."""

import json
from pathlib import Path

import pytest
from page_to_markdown.cli import main
from page_to_markdown.outline import list_headings


# Local HTML used by every heading command test.
FIXTURES = Path(__file__).parents[1] / "fixtures"


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
