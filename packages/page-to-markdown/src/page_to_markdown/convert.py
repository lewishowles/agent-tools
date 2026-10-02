"""Convert the selected lightweight HTML DOM into compact Markdown."""

import re
from urllib.parse import urljoin

from page_to_markdown.select import _DomBuilder, _DomNode

# HTML elements that start block-level Markdown paragraphs.
BLOCK_TAGS = frozenset(
	[
		"address",
		"article",
		"blockquote",
		"body",
		"dd",
		"details",
		"div",
		"dl",
		"dt",
		"figure",
		"figcaption",
		"h1",
		"h2",
		"h3",
		"h4",
		"h5",
		"h6",
		"head",
		"hr",
		"html",
		"li",
		"main",
		"ol",
		"p",
		"pre",
		"section",
		"summary",
		"table",
		"ul",
	]
)

# HTML elements omitted from Markdown output.
IGNORED_TAGS = frozenset(
	["button", "head", "input", "label", "link", "meta", "template", "title"]
)

# Pattern that identifies a language class on a code element.
LANGUAGE_PATTERN = re.compile(r"(?:^|\s)language-([^\s]+)")
# Pattern used to find the longest backtick run in inline code.
BACKTICK_PATTERN = re.compile(r"`+")
# Pattern used to collapse whitespace in rendered text.
WHITESPACE_PATTERN = re.compile(r"\s+")


def convert_to_markdown(html: str, base_url: str | None = None) -> str:
	"""Convert HTML into Markdown, resolving links against an optional base URL.

	Returns an empty string for empty or non-string input. When content is
	present, the returned Markdown ends with one newline.
	"""
	if not isinstance(html, str) or not html.strip():
		return ""

	builder = _DomBuilder()
	builder.feed(html)
	builder.close()

	blocks = _render_blocks(builder.root, base_url)
	if not blocks:
		return ""

	return "\n\n".join(block.strip("\n") for block in blocks) + "\n"


def _render_blocks(node: _DomNode, base_url: str | None) -> list[str]:
	"""Render a node's children as a sequence of Markdown blocks."""
	blocks = []
	inline_parts = []

	def flush_inline():
		"""Normalise the buffered inline content into a block if any, then clear the buffer."""
		inline = _normalise_inline("".join(inline_parts))
		inline_parts.clear()
		if inline:
			blocks.append(inline)

	for child in node.children:
		if child.tag == "#text":
			inline_parts.append(_render_inline(child, base_url))
		elif child.tag in IGNORED_TAGS:
			continue
		elif child.tag in BLOCK_TAGS:
			flush_inline()
			blocks.extend(_render_block(child, base_url))
		else:
			inline_parts.append(_render_inline(child, base_url))

	flush_inline()
	return blocks


def _render_block(node: _DomNode, base_url: str | None) -> list[str]:
	"""Render one block-level node."""
	tag = node.tag

	if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
		content = _normalise_inline(_render_inline_children(node, base_url))
		if not content:
			return []
		return [f"{'#' * int(tag[1:])} {content}"]

	if tag == "p":
		content = _normalise_inline(_render_inline_children(node, base_url))
		return [content] if content else []

	if tag in {"ul", "ol"}:
		content = _render_list(node, base_url)
		return [content] if content else []

	if tag == "blockquote":
		content = _render_blocks(node, base_url)
		if not content:
			return []
		quoted = "\n\n".join(content)
		return ["\n".join(f"> {line}" if line else ">" for line in quoted.split("\n"))]

	if tag == "pre":
		return [_render_code_block(node)]

	if tag == "table":
		content = _render_table(node, base_url)
		return [content] if content else []

	if tag == "hr":
		return ["---"]

	return _render_blocks(node, base_url)


def _render_inline(node: _DomNode, base_url: str | None) -> str:
	"""Render inline content while preserving meaningful text spacing."""
	if node.tag == "#text":
		return WHITESPACE_PATTERN.sub(" ", node.text)

	tag = node.tag
	if tag in IGNORED_TAGS:
		return ""

	if tag == "a":
		content = _normalise_inline(_render_inline_children(node, base_url))
		href = node.attrs.get("href")
		if not content:
			return ""
		if not href:
			return content
		if base_url is not None:
			href = urljoin(base_url, href)
		return f"[{content}]({href})"

	if tag in {"strong", "b"}:
		content = _normalise_inline(_render_inline_children(node, base_url))
		return f"**{content}**" if content else ""

	if tag in {"em", "i"}:
		content = _normalise_inline(_render_inline_children(node, base_url))
		return f"*{content}*" if content else ""

	if tag == "code":
		return _render_inline_code(_raw_text(node))

	if tag == "br":
		return "  \n"

	if tag == "img":
		alt = node.attrs.get("alt", "").strip()
		src = node.attrs.get("src", "").strip()
		if not alt:
			return ""
		if src and base_url is not None:
			src = urljoin(base_url, src)
		return f"![{alt}]({src})" if src else alt

	return _render_inline_children(node, base_url)


def _render_inline_children(node: _DomNode, base_url: str | None) -> str:
	"""Render all children of an element as inline Markdown."""
	return "".join(_render_inline(child, base_url) for child in node.children)


def _normalise_inline(content: str) -> str:
	"""Collapse HTML whitespace and trim block-edge whitespace."""
	content = content.replace("\r\n", "\n").replace("\r", "\n")
	return content.strip()


def _raw_text(node: _DomNode) -> str:
	"""Return descendant text without collapsing whitespace."""
	if node.tag == "#text":
		return node.text
	return "".join(_raw_text(child) for child in node.children)


def _render_inline_code(content: str) -> str:
	"""Wrap inline code in a backtick fence that cannot close its content."""
	content = WHITESPACE_PATTERN.sub(" ", content).strip()
	if not content:
		return ""

	longest_run = max(
		(len(match.group(0)) for match in BACKTICK_PATTERN.finditer(content)),
		default=0,
	)
	fence = "`" * max(1, longest_run + 1)
	padding = " " if content.startswith("`") or content.endswith("`") else ""
	return f"{fence}{padding}{content}{padding}{fence}"


def _render_code_block(node: _DomNode) -> str:
	"""Render a preformatted block with an optional language fence."""
	code_node = _first_descendant(node, "code")
	source_node = code_node or node
	content = _raw_text(source_node).replace("\r\n", "\n").replace("\r", "\n")
	content = content.strip("\n")

	language = ""
	if code_node is not None:
		match = LANGUAGE_PATTERN.search(code_node.attrs.get("class", ""))
		if match:
			language = match.group(1)

	longest_run = max(
		(len(match.group(0)) for match in BACKTICK_PATTERN.finditer(content)),
		default=0,
	)
	fence = "`" * max(3, longest_run + 1)
	return f"{fence}{language}\n{content}\n{fence}"


def _first_descendant(node: _DomNode, tag: str) -> _DomNode | None:
	"""Find the first descendant with a given tag."""
	for child in node.children:
		if child.tag == tag:
			return child
		if child.tag != "#text":
			result = _first_descendant(child, tag)
			if result is not None:
				return result
	return None


def _render_list(node: _DomNode, base_url: str | None, indent: str = "") -> str:
	"""Render a list and indent nested lists beneath their parent marker."""
	lines = []
	item_number = 1

	for child in node.children:
		if child.tag != "li":
			continue

		content_parts = []
		nested_lists = []
		for item_child in child.children:
			if item_child.tag in {"ul", "ol"}:
				nested_lists.append(item_child)
			elif item_child.tag == "p":
				content_parts.append(_render_inline_children(item_child, base_url))
			else:
				content_parts.append(_render_inline(item_child, base_url))

		content = _normalise_inline("".join(content_parts))
		marker = f"{item_number}. " if node.tag == "ol" else "- "
		lines.append(f"{indent}{marker}{content}".rstrip())
		item_number += 1

		for nested_list in nested_lists:
			nested = _render_list(nested_list, base_url, indent + " " * len(marker))
			if nested:
				lines.extend(nested.split("\n"))

	return "\n".join(lines)


def _render_table(node: _DomNode, base_url: str | None) -> str:
	"""Render a simple table using its first row as the Markdown header."""
	rows = []
	for row in _table_rows(node):
		cells = [
			_normalise_table_cell(_render_inline_children(cell, base_url))
			for cell in row.children
			if cell.tag in {"th", "td"}
		]
		if cells:
			rows.append(cells)

	if not rows:
		return ""

	column_count = max(len(row) for row in rows)
	padded_rows = [row + [""] * (column_count - len(row)) for row in rows]
	lines = [
		_format_table_row(padded_rows[0]),
		_format_table_row(["---"] * column_count),
	]
	lines.extend(_format_table_row(row) for row in padded_rows[1:])
	return "\n".join(lines)


def _table_rows(node: _DomNode) -> list[_DomNode]:
	"""Collect table rows without descending into nested tables."""
	rows = []
	for child in node.children:
		if child.tag == "tr":
			rows.append(child)
		elif child.tag not in {"#text", "table"}:
			rows.extend(_table_rows(child))
	return rows


def _normalise_table_cell(content: str) -> str:
	"""Keep table cells on one line and escape Markdown column separators."""
	return content.replace("\n", " ").replace("|", "\\|").strip()


def _format_table_row(cells: list[str]) -> str:
	"""Format one row using the conventional pipe-table shape."""
	return "| " + " | ".join(cells) + " |"
