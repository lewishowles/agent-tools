from pathlib import Path

import pytest
from page_to_markdown.cli import main
from page_to_markdown.convert import convert_to_markdown
from page_to_markdown.fetch import read_file
from page_to_markdown.report import build_metadata, build_report
from page_to_markdown.select import select_content

# Keep extraction tests entirely on checked-in fixture files.
FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_fixture(name: str) -> str:
	"""Read a local HTML fixture through the production file loader."""
	return read_file(FIXTURES / name)


def extract_fixture(name: str, base_url: str | None = None) -> tuple[str, str, str]:
	"""Run the local fixture through reading, selection, and conversion."""
	raw_html = load_fixture(name)
	selected_html = select_content(raw_html)
	markdown = convert_to_markdown(selected_html, base_url=base_url)
	return raw_html, selected_html, markdown


@pytest.mark.parametrize(
	"fixture_name",
	[
		"simple.html",
		"blog-with-chrome.html",
		"docs-page-with-code.html",
		"table-heavy.html",
		"js-app-shell.html",
		"relative-links.html",
		"short-page.html",
		"valueless-attributes.html",
		"inline-code-with-tag-text.html",
	],
)
def test_every_fixture_runs_through_local_extraction_pipeline(
	fixture_name: str,
) -> None:
	raw_html, selected_html, _ = extract_fixture(fixture_name)

	assert raw_html
	assert selected_html


def test_simple_fixture_preserves_full_body_content() -> None:
	_, _, markdown = extract_fixture("simple.html")

	assert markdown == "# Hello\n\nThis is a test page.\n"


def test_blog_fixture_removes_page_chrome_and_reports_selection() -> None:
	raw_html, selected_html, markdown = extract_fixture("blog-with-chrome.html")
	report = build_report(raw_html, selected_html, markdown, "blog-with-chrome.html")

	assert "Understanding content selection" in markdown
	assert "Welcome to my blog" not in markdown
	assert "Subscribe to our newsletter" not in markdown
	assert "Copyright 2024 My Blog" not in markdown
	assert report.selected_content_root == "main"
	assert report.removed_elements_total >= 1
	assert report.verdict == "high-confidence"


def test_docs_fixture_matches_checked_in_markdown() -> None:
	_, selected_html, markdown = extract_fixture("docs-page-with-code.html")
	expected_markdown = read_file(FIXTURES / "docs-page-with-code.expected.md")

	assert selected_html
	assert markdown == expected_markdown


def test_table_fixture_matches_checked_in_markdown() -> None:
	_, selected_html, markdown = extract_fixture("table-heavy.html")
	expected_markdown = read_file(FIXTURES / "table-heavy.expected.md")

	assert selected_html
	assert markdown == expected_markdown


def test_relative_links_resolve_against_the_document_url() -> None:
	_, _, markdown = extract_fixture(
		"relative-links.html", base_url="https://example.com/docs/page.html"
	)

	assert (
		"[getting started guide](https://example.com/guide/getting-started.html)"
		in markdown
	)
	assert "[reference](https://example.com/reference)" in markdown


@pytest.mark.parametrize(
	("fixture_name", "reason"),
	[
		("js-app-shell.html", "JS app shell detected"),
		("short-page.html", "converted Markdown is empty or very short"),
	],
)
def test_low_confidence_fixtures_include_actionable_report_reasons(
	fixture_name: str, reason: str
) -> None:
	raw_html, selected_html, markdown = extract_fixture(fixture_name)
	report = build_report(raw_html, selected_html, markdown, fixture_name)

	assert report.verdict == "low-confidence"
	assert any(reason in candidate for candidate in report.reasons)


def test_inline_code_with_escaped_tag_text_does_not_swallow_following_content() -> None:
	"""`<title>`/`<textarea>` are RCDATA in html.parser: unescaped text re-fed to
	the parser would be read as a real tag and swallow the rest of the
	document as raw text until a closing tag that never arrives."""
	_, selected_html, markdown = extract_fixture("inline-code-with-tag-text.html")

	assert "<title>" not in selected_html
	assert markdown == (
		"# Escaped tags inside inline code\n\n"
		"Start a page with `<title>` in the head.\n\n"
		"This paragraph must survive after the code sample above.\n"
	)


def test_valueless_img_attributes_do_not_crash_conversion() -> None:
	_, _, markdown = extract_fixture("valueless-attributes.html")

	assert "![Company logo](logo.png)" in markdown


def test_local_fixture_metadata_contains_title_without_inventing_a_url() -> None:
	raw_html, selected_html, _ = extract_fixture("simple.html")
	metadata = build_metadata(raw_html, selected_html, "simple.html")

	assert metadata["title"] == "Simple fixture"
	assert metadata["url"] is None
	assert metadata["timestamp"]


def test_main_combines_two_fixture_sources_with_document_blocks(capsys) -> None:
	exit_code = main(
		[
			str(FIXTURES / "simple.html"),
			str(FIXTURES / "table-heavy.html"),
		]
	)
	captured = capsys.readouterr()

	assert exit_code == 0
	assert "## Simple fixture" in captured.out
	assert "## Table-heavy reference" in captured.out
	assert "Source: " + str(FIXTURES / "simple.html") in captured.out
	assert "\n\n---\n\n" in captured.out
	assert captured.out.index("## Simple fixture") < captured.out.index(
		"## Table-heavy reference"
	)


def test_main_single_source_preserves_exact_markdown_output(capsys) -> None:
	exit_code = main([str(FIXTURES / "docs-page-with-code.html")])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.out == read_file(FIXTURES / "docs-page-with-code.expected.md")


def test_main_confidence_report_renders_aligned_rows(capsys, monkeypatch) -> None:
	monkeypatch.setenv("TERM", "xterm")

	exit_code = main([str(FIXTURES / "simple.html"), "--confidence"])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert "Selected content root  body" in captured.out
	assert "× Verdict              low-confidence" in captured.out


def test_main_keeps_failed_source_in_batch_output(tmp_path, capsys) -> None:
	missing_source = tmp_path / "missing.html"
	good_source = FIXTURES / "simple.html"

	exit_code = main([str(missing_source), str(good_source)])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert f"## Failed: {missing_source}" in captured.out
	assert "**Failed to fetch:**" in captured.out
	assert "## Simple fixture" in captured.out
	assert f"Source   {missing_source}" in captured.err
	assert "Error " in captured.err


def test_main_returns_failure_without_output_when_all_sources_fail(
	tmp_path, capsys
) -> None:
	missing_sources = [
		str(tmp_path / "missing-one.html"),
		str(tmp_path / "missing-two.html"),
	]

	exit_code = main(missing_sources)
	captured = capsys.readouterr()

	assert exit_code == 1
	assert captured.out == ""
	assert all(f"Source   {source}" in captured.err for source in missing_sources)


def test_main_uses_source_heading_when_batch_document_has_no_title_or_heading(
	capsys,
) -> None:
	email_source = FIXTURES / "minimal-email.html"

	exit_code = main([str(email_source), str(FIXTURES / "simple.html")])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert f"## {email_source}" in captured.out
