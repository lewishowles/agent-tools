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
