from pathlib import Path

import pytest

from page_to_markdown import style
from page_to_markdown.style import _plain_row_group


def test_plain_row_group_aligns_values_for_mixed_result_markers() -> None:
	rows = [
		{"label": "Source", "value": "https://example.com"},
		{"label": "Links", "value": "3"},
		{"label": "Finding", "value": "needs review", "result": "failed"},
		{"label": "Verdict", "value": "high-confidence", "result": "success"},
	]

	rendered = _plain_row_group(rows)
	lines = rendered.splitlines()
	value_columns = {
		line.index(row["value"]) for line, row in zip(lines, rows, strict=True)
	}

	assert value_columns == {12}
	assert rendered == (
		"Source      https://example.com\n"
		"Links       3\n"
		"x Finding   needs review\n"
		"OK Verdict  high-confidence"
	)


def test_renderers_keep_page_to_markdown_plain_text_when_cli_style_is_missing(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	monkeypatch.setenv("PATH", str(tmp_path))

	rows = [
		{"label": "Source", "value": "docs.html"},
		{"label": "Verdict", "value": "high-confidence", "result": "success"},
	]

	assert style.status("success", "Saved", "draft 1") == "OK Saved draft 1"
	assert style.hint("Run show") == "i Hint: Run show"
	assert style.row("Draft", "abc", "success") == "OK Draft  abc"
	assert style.row_group(rows) == "Source      docs.html\nOK Verdict  high-confidence"
	assert style.span("saved", "success") == "saved"
