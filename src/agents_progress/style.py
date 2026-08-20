"""Render progress CLI output through the cli-style binary when available."""

from collections.abc import Callable

from agents_progress._cli_style import (
	CliStyleNotFoundError,
	hint as render_hint,
	render as render_generic,
	row as render_row,
	span as render_span,
	status as render_status,
)


# Match cli-style's plain-text result markers when its binary is unavailable.
_PLAIN_RESULT_MARKERS = {
	"failed": "x",
	"info": ">",
	"skipped": "-",
	"success": "OK",
	"warning": "!",
}


def _render_or_plain(
	styled_renderer: Callable[[], str],
	plain_renderer: Callable[[], str],
) -> str:
	"""Render with cli-style, falling back when its binary is unavailable."""
	try:
		return styled_renderer()
	except CliStyleNotFoundError:
		return plain_renderer()


def _plain_result_marker(result: str) -> str:
	"""Return cli-style's unstyled marker for a result type."""
	return _PLAIN_RESULT_MARKERS.get(result, ">")


def _plain_row(
	label: str,
	value: str,
	result: str = "",
	label_width: int | None = None,
) -> str:
	"""Render a labelled value without ANSI styling."""
	displayed_label = f"{_plain_result_marker(result)} {label}" if result else label
	if label_width is not None:
		displayed_label = displayed_label.ljust(label_width)

	return f"{displayed_label}  {value}"


def _plain_row_group(rows: list[dict[str, str]]) -> str:
	"""Render aligned labelled values without ANSI styling."""
	displayed_labels = [
		(
			f"{_plain_result_marker(item.get('result', ''))} {item['label']}"
			if item.get("result")
			else item["label"]
		)
		for item in rows
	]
	label_width = max((len(label) for label in displayed_labels), default=0)

	return "\n".join(
		_plain_row(
			item["label"],
			item["value"],
			item.get("result", ""),
			label_width,
		)
		for item in rows
	)


def _plain_status(result_type: str, label: str, detail: str) -> str:
	"""Render a status line without ANSI styling."""
	marker = _plain_result_marker(result_type)
	status_label = f"{marker} {label}" if label else marker
	return f"{status_label} {detail}".rstrip()


def _plain_hint(message: str) -> str:
	"""Render a hint line without ANSI styling."""
	return f"i Hint: {message}"


def status(result_type: str, label: str = "", detail: str = "") -> str:
	"""Render a status line using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_status(result_type, label, detail),
		lambda: _plain_status(result_type, label, detail),
	)


def hint(message: str) -> str:
	"""Render a hint line using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_hint(message),
		lambda: _plain_hint(message),
	)


def row(label: str, value: str, result: str = "") -> str:
	"""Render a labelled row using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_row(label, value, result),
		lambda: _plain_row(label, value, result),
	)


def row_group(rows: list[dict[str, str]]) -> str:
	"""Render aligned labelled rows using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_generic("row-group", {"rows": rows}),
		lambda: _plain_row_group(rows),
	)


def span(value: str, tone: str = "info") -> str:
	"""Render an inline span using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_span(value, tone),
		lambda: value,
	)
