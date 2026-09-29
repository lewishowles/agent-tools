"""Render friction CLI output through cli-style when it is installed."""

from collections.abc import Callable

from ._cli_style import CliStyleNotFoundError
from ._cli_style import row as render_row
from ._cli_style import span as render_span
from ._cli_style import status as render_status
from ._cli_style import table as render_table


def _render_or_plain(
	styled_renderer: Callable[[], str],
	plain_renderer: Callable[[], str],
) -> str:
	"""Render with cli-style, falling back to plain terminal text when absent."""
	try:
		return styled_renderer()
	except CliStyleNotFoundError:
		return plain_renderer()


def span(value: str, tone: str = "info") -> str:
	"""Render one inline value through cli-style or return its plain text."""
	return _render_or_plain(
		lambda: render_span(value, tone),
		lambda: value,
	)


def row(label: str, value: str, result: str = "") -> str:
	"""Render one labelled value through cli-style or a plain equivalent."""
	return _render_or_plain(
		lambda: render_row(label, value, result),
		lambda: f"{label}  {value}",
	)


def _plain_table(
	columns: list[dict[str, object]], rows: list[dict[str, object]]
) -> str:
	"""Render columns and rows as a padded plain-text table."""
	if not columns or not rows:
		return ""

	keys = [str(column["key"]) for column in columns]
	labels = [str(column["label"]) for column in columns]
	widths = [
		max(len(label), *(len(str(row.get(key, ""))) for row in rows))
		for key, label in zip(keys, labels, strict=True)
	]

	def render_line(values: list[str]) -> str:
		"""Pad every table value except the final cell."""
		return "  ".join(
			value.ljust(width) if index < len(values) - 1 else value
			for index, (value, width) in enumerate(zip(values, widths, strict=True))
		)

	header = render_line(labels)
	rule = render_line(["-" * width for width in widths])
	body = [render_line([str(row.get(key, "")) for key in keys]) for row in rows]
	return "\n".join([header, rule, *body])


def table(columns: list[dict[str, object]], rows: list[dict[str, object]]) -> str:
	"""Render column-aligned rows through cli-style or plain text."""
	return _render_or_plain(
		lambda: render_table(columns, rows),
		lambda: _plain_table(columns, rows),
	)


def status(result: str, label: str, detail: str) -> str:
	"""Render one status line through cli-style or a plain equivalent."""
	return _render_or_plain(
		lambda: render_status(result, label, detail),
		lambda: f"{label} {detail}".rstrip(),
	)
