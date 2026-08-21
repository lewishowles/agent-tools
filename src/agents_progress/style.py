"""Render progress CLI output through the cli-style binary when available."""

from collections.abc import Callable

from agents_progress._cli_style import (
	CliStyleNotFoundError,
	divider as render_divider,
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

# Divider width used when no width is given, for plain-text output.
_PLAIN_DIVIDER_WIDTH = 40


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


def _plain_table(columns: list[dict[str, str]], rows: list[dict[str, str]]) -> str:
	"""Render columns and rows as a plain-text table, padding each column to its widest value."""
	if not columns or not rows:
		return ""

	keys = [column["key"] for column in columns]
	labels = [column["label"] for column in columns]
	widths = [
		max(len(label), *(len(row.get(key, "")) for row in rows))
		for key, label in zip(keys, labels, strict=True)
	]

	def render_line(values: list[str]) -> str:
		return "  ".join(
			value.ljust(width) if index < len(values) - 1 else value
			for index, (value, width) in enumerate(zip(values, widths, strict=True))
		)

	header = render_line(labels)
	rule = render_line(["-" * width for width in widths])
	body = [render_line([row.get(key, "") for key in keys]) for row in rows]
	return "\n".join([header, rule, *body])


def _plain_status(result_type: str, label: str, detail: str) -> str:
	"""Render a status line without ANSI styling."""
	marker = _plain_result_marker(result_type)
	status_label = f"{marker} {label}" if label else marker
	return f"{status_label} {detail}".rstrip()


def _plain_hint(message: str) -> str:
	"""Render a hint line without ANSI styling."""
	return f"i Hint: {message}"


def _plain_labelled_line(label: str, message: str) -> str:
	"""Render a labelled line without ANSI styling."""
	return f"i {label}: {message}"


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


def labelled_line(label: str, message: str) -> str:
	"""Render a labelled line using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_generic("labelled-line", {"label": label, "message": message}),
		lambda: _plain_labelled_line(label, message),
	)


def row(label: str, value: str, result: str = "") -> str:
	"""Render a labelled row using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_row(label, value, result),
		lambda: _plain_row(label, value, result),
	)


def _plain_divider(label: str, divider_width: int | None) -> str:
	"""Render a horizontal divider without ANSI styling."""
	width = divider_width if divider_width is not None else _PLAIN_DIVIDER_WIDTH
	line = "-" * max(width, 0)
	return f"{label} {line}" if label else line


def divider(
	label: str = "",
	divider_width: int | None = None,
	divider_colour: str | None = None,
	label_colour: str | None = None,
) -> str:
	"""Render a divider using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_divider(
			label,
			divider_width,
			divider_colour,
			label_colour,
		),
		lambda: _plain_divider(label, divider_width),
	)


def row_group(rows: list[dict[str, str]]) -> str:
	"""Render aligned labelled rows using cli-style or plain text; the cli-style path wraps values at 72 columns."""
	return _render_or_plain(
		lambda: render_generic("row-group", {"rows": rows, "wrapWidth": 72}),
		lambda: _plain_row_group(rows),
	)


def table(columns: list[dict[str, str]], rows: list[dict[str, str]]) -> str:
	"""Render column-aligned rows using cli-style or plain text; a row missing a column key renders that cell blank."""
	return _render_or_plain(
		lambda: render_generic("table", {"columns": columns, "rows": rows}),
		lambda: _plain_table(columns, rows),
	)


def span(value: str, tone: str = "info", weight: str | None = None) -> str:
	"""Render an inline span using cli-style or plain text."""
	return _render_or_plain(
		lambda: render_span(value, tone, weight=weight),
		lambda: value,
	)
