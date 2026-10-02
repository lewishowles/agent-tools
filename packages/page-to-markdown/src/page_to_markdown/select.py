"""Content selection for page-to-markdown.

Strips page chrome (script, style, nav, header, footer, etc.) and selects
the main content region using deterministic rules: main, then article,
then [role=main], followed by the remaining body content or root.

Uses only the standard library (html.parser) to preserve nested markup
for later Markdown conversion.
"""

from html import escape
from html.parser import HTMLParser

# Elements to strip entirely (content and tags).
STRIP_TAGS = frozenset(
	["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]
)

# Void (self-closing) elements that never have children.
VOID_TAGS = frozenset(
	[
		"area",
		"base",
		"br",
		"col",
		"embed",
		"hr",
		"img",
		"input",
		"link",
		"meta",
		"source",
		"track",
		"wbr",
	]
)


class _DomNode:
	"""A lightweight DOM node built by _DomBuilder."""

	# Store only the fields needed to represent an extracted DOM node.
	__slots__ = ("tag", "attrs", "children", "parent", "text")

	def __init__(self, tag, attrs=None, parent=None):
		"""Initialise a node with optional attributes and parent linkage."""
		# Element name used by selection and Markdown rendering.
		self.tag = tag
		# HTMLParser reports valueless attributes (e.g. `<img alt>`) with a
		# None value; normalise to "" so every consumer can treat attrs as strings.
		self.attrs = {name: value or "" for name, value in attrs} if attrs else {}
		# Child nodes kept in source order.
		self.children = []
		# Parent link used while building and inspecting the tree.
		self.parent = parent
		# Text stored directly on text nodes.
		self.text = ""

	def add_text(self, text):
		"""Append parsed text as a child text node."""
		node = _DomNode("#text", parent=self)
		node.text = text
		self.children.append(node)

	@property
	def full_text(self):
		"""All direct and descendant text, concatenated."""
		parts = [self.text]
		for child in self.children:
			parts.append(child.full_text)
		return "".join(parts)

	@property
	def text_length(self):
		"""Length of all descendant text, excluding whitespace."""
		return len(self.full_text.strip())

	@property
	def tag_count(self):
		"""Count of all descendant element nodes (not #text)."""
		return sum(1 + c.tag_count for c in self.children if c.tag != "#text")


class _DomBuilder(HTMLParser):
	"""Builds a lightweight DOM tree from HTML using html.parser."""

	def __init__(self):
		"""Initialise an empty DOM tree and parser state."""
		super().__init__(convert_charrefs=True)
		# Root node for the parsed document.
		self.root = _DomNode("#root")
		# Open element stack used to attach parsed nodes.
		self._stack = [self.root]
		# Depth marker for content inside stripped elements.
		self._strip_depth = 0

	def handle_starttag(self, tag, attrs):
		"""Add a start tag unless it or an ancestor is being stripped."""
		tag = tag.lower()

		# If inside a stripped element, ignore everything.
		if self._strip_depth > 0:
			if tag not in VOID_TAGS:
				self._strip_depth += 1
			return

		if tag in STRIP_TAGS:
			self._strip_depth = 1
			return

		node = _DomNode(tag, attrs, self._stack[-1])
		self._stack[-1].children.append(node)
		if tag not in VOID_TAGS:
			self._stack.append(node)

	def handle_startendtag(self, tag, attrs):
		"""Handle self-closing tags like <img/>."""
		tag = tag.lower()
		if self._strip_depth > 0:
			return
		if tag in STRIP_TAGS:
			return
		node = _DomNode(tag, attrs, self._stack[-1])
		self._stack[-1].children.append(node)

	def handle_endtag(self, tag):
		"""Close matching open content or reduce stripped-content tracking."""
		tag = tag.lower()
		if self._strip_depth > 0:
			if tag not in VOID_TAGS:
				self._strip_depth -= 1
			return

		# Pop stack until we find the matching tag (handles implicit closes).
		for i in range(len(self._stack) - 1, 0, -1):
			if self._stack[i].tag == tag:
				del self._stack[i:]
				break

	def handle_data(self, data):
		"""Append parsed text to the current node unless content is stripped."""
		if self._strip_depth > 0:
			return
		self._stack[-1].add_text(data)


def _serialise(node):
	"""Serialise a DOM node back to HTML, preserving nested markup.

	Text and attribute values are re-escaped because the parser decodes
	entities on the way in (e.g. `&lt;title&gt;`); without escaping, decoded
	text like `<title>` would be re-parsed as a real tag downstream, and
	`<title>`/`<textarea>` are RCDATA elements that swallow the rest of the
	document as text until a matching close tag is found.
	"""
	parts = []
	for child in node.children:
		if child.tag == "#text":
			parts.append(escape(child.text, quote=False))
			continue

		# _DomNode never stores None for an attribute value (see __init__), so
		# a valueless attribute like `alt` always serialises as `alt=""`.
		attrs_str = "".join(f' {k}="{escape(v)}"' for k, v in child.attrs.items())

		if child.tag in VOID_TAGS:
			parts.append(f"<{child.tag}{attrs_str}>")
		else:
			inner = _serialise(child)
			parts.append(f"<{child.tag}{attrs_str}>{inner}</{child.tag}>")

	return "".join(parts)


def _find_first(root, predicate):
	"""Depth-first search for the first node matching predicate."""
	for child in root.children:
		if predicate(child):
			return child
		result = _find_first(child, predicate)
		if result is not None:
			return result
	return None


def _find_all(root, predicate, results=None):
	"""Find all nodes matching predicate, depth-first."""
	if results is None:
		results = []
	for child in root.children:
		if predicate(child):
			results.append(child)
		_find_all(child, predicate, results)
	return results


def select_content(html):
	"""Select semantic content, falling back to the body or root if absent."""
	builder = _DomBuilder()
	builder.feed(html)
	builder.close()
	root = builder.root

	main = _find_first(root, lambda n: n.tag == "main")
	if main:
		return _serialise(main)

	article = _find_first(root, lambda n: n.tag == "article")
	if article:
		return _serialise(article)

	role_main = _find_first(root, lambda n: n.attrs.get("role", "").lower() == "main")
	if role_main:
		return _serialise(role_main)

	body = _find_first(root, lambda n: n.tag == "body")
	return _serialise(body or root)
