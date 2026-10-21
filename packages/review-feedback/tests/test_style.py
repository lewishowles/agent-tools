from pathlib import Path

import pytest

from review_feedback import style


def test_renderers_keep_review_feedback_plain_text_when_cli_style_is_missing(
	monkeypatch: pytest.MonkeyPatch,
	tmp_path: Path,
) -> None:
	monkeypatch.setenv("PATH", str(tmp_path))

	rows = [
		{"label": "A", "value": "1"},
		{"label": "Bee", "value": "2", "result": "info"},
	]

	assert style.status("success", "Saved", "draft 1") == "OK Saved draft 1"
	assert style.hint("Run show") == "i Hint: Run show"
	assert style.row("Draft", "abc", "success") == "OK Draft  abc"
	assert style.row_group(rows) == "A  1\n> Bee  2"
