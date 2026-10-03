"""Find the headings in converted Markdown so an agent can pick one by selector."""

import re
from dataclasses import dataclass
from difflib import get_close_matches


# Matches a heading line written with one to six leading # marks, capturing the marks and the heading text.
HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
# Matches a code fence: three or more backticks or tildes, indented by no more than three spaces.
FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# Matches a link or image, capturing the visible text so the destination can be dropped.
LINK_PATTERN = re.compile(r"!?\[([^\]]+)\]\([^)]*\)")
# Matches an inline code span wrapped in one or more backticks, capturing the code inside.
CODE_PATTERN = re.compile(r"(`+)(.*?)\1")
# Matches paired one- to three-asterisk emphasis markers around visible text.
EMPHASIS_PATTERN = re.compile(r"(?<!\*)(\*{1,3})([^\s*](?:.*?[^\s*])??)\1(?!\*)")
# The error kind, shown in JSON output, when no heading matches the query.
MISSING_HEADING = "missing"
# The error kind, shown in JSON output, when the query text matches more than one heading.
AMBIGUOUS_HEADING = "ambiguous"


@dataclass(frozen=True)
class Heading:
	"""Describe one heading in the converted Markdown and the selector that picks it."""

	# The heading level, from 1 for "#" to 6 for "######".
	level: int
	# The heading text as a reader sees it, without link syntax, backticks, or paired "*" emphasis.
	text: str
	# The GitHub-style anchor for the heading, unique within the document.
	selector: str
	# The zero-based line where this heading starts in the converted Markdown.
	line: int


class HeadingLookupError(ValueError):
	"""Report a heading query that matched no heading or more than one.

	The error keeps its kind ("missing" or "ambiguous"), the query as typed, and up to five
	headings the user could select instead.
	"""

	def __init__(self, kind: str, query: str, candidates: list[Heading]) -> None:
		"""Store the failure kind, the query, and the first five suggested headings."""
		super().__init__(kind)
		self.kind = kind
		self.query = query
		self.candidates = candidates[:5]


def list_headings(markdown: str) -> list[Heading]:
	"""Return the headings in the order they appear, ignoring lines inside fenced code blocks.

	Repeated headings get selectors the way GitHub numbers its anchors: the first keeps the plain
	anchor and later ones gain "-1", "-2", and so on, skipping any number another heading already uses.
	"""
	# The headings found so far, in document order.
	headings = []
	# The selectors already given to earlier headings.
	used_selectors = set()
	# The backtick or tilde that opened the current code block, or None outside a code block.
	fence_character = None
	# How many fence characters opened the current code block.
	fence_length = 0

	for line_number, line in enumerate(markdown.splitlines()):
		# The code fence at the start of this line, if there is one.
		fence_match = FENCE_PATTERN.match(line)

		# A code block ends only at a fence of the same character, at least as long as the opening
		# fence, with nothing after it. A shorter or different fence is part of the code.
		if fence_character is not None:
			if (
				fence_match
				and fence_match.group(1)[0] == fence_character
				and len(fence_match.group(1)) >= fence_length
				and not line[fence_match.end() :].strip()
			):
				fence_character = None
			continue

		if fence_match:
			fence_character = fence_match.group(1)[0]
			fence_length = len(fence_match.group(1))
			continue

		# The heading at the start of this line, if there is one.
		heading_match = HEADING_PATTERN.match(line)
		if not heading_match:
			continue

		# The visible heading text. A heading made only of formatting, such as "# **", has nothing
		# to select and is left out.
		plain_text = _plain_text(heading_match.group(2))
		if not plain_text:
			continue

		# The anchor for the heading text, before any number is added for a repeat.
		base_selector = _selector(plain_text)
		# The anchor given to this heading.
		selector = base_selector
		# The next number to try when the anchor is already taken.
		suffix = 1
		while selector in used_selectors:
			selector = f"{base_selector}-{suffix}"
			suffix += 1

		used_selectors.add(selector)
		headings.append(
			Heading(
				level=len(heading_match.group(1)),
				text=plain_text,
				selector=selector,
				line=line_number,
			)
		)

	return headings


def extract_section(
	markdown: str, query: str, without_children: bool = False
) -> tuple[Heading, str]:
	"""Return the heading that the query selects, with its section of the Markdown.

	The query is tried as an anchor selector first, with or without a leading "#", and then
	as heading text, ignoring letter case, repeated spaces, and inline Markdown. The section
	runs from the heading line to the next heading at the same or a higher level. When
	without_children is set, it stops at the next heading of any level instead.

	Raises HeadingLookupError when no heading matches, or when the text matches several.
	"""
	headings = list_headings(markdown)
	selector_query = query.removeprefix("#")
	selected = next(
		(heading for heading in headings if heading.selector == selector_query), None
	)

	if selected is None:
		normalised_query = _plain_text(query).casefold()
		matches = [
			heading
			for heading in headings
			if heading.text.casefold() == normalised_query
		]

		if len(matches) > 1:
			raise HeadingLookupError(AMBIGUOUS_HEADING, query, matches)

		if not matches:
			nearby_text = get_close_matches(
				normalised_query,
				# One entry per distinct heading text, so repeated headings count once towards the five.
				dict.fromkeys(heading.text.casefold() for heading in headings),
				n=5,
			)
			candidates = []
			for text in nearby_text:
				candidates.extend(
					heading for heading in headings if heading.text.casefold() == text
				)

			raise HeadingLookupError(MISSING_HEADING, query, candidates)

		selected = matches[0]

	lines = markdown.splitlines(keepends=True)
	end_line = len(lines)
	for heading in headings:
		if heading.line <= selected.line:
			continue

		if without_children or heading.level <= selected.level:
			end_line = heading.line
			break

	return selected, "".join(lines[selected.line : end_line])


def _plain_text(markdown: str) -> str:
	"""Return the text a reader sees in a heading.

	Keeps the visible text of links and images, removes code span backticks but keeps the code
	exactly as written, removes paired one- to three-asterisk emphasis markers repeatedly, preserves
	lone asterisks, and collapses repeated whitespace to single spaces.
	"""
	text = LINK_PATTERN.sub(r"\1", markdown)
	# Asterisks inside code spans are swapped for a null character, which converted pages never
	# contain, so removing emphasis leaves them alone. They are restored at the end.
	asterisk_marker = "\x00"
	text = CODE_PATTERN.sub(
		lambda match: match.group(2).replace("*", asterisk_marker), text
	)
	# The text before the latest removal. Nested emphasis such as "*a **b** c*" needs more than one pass.
	previous_text = None
	while previous_text != text:
		previous_text = text
		text = EMPHASIS_PATTERN.sub(r"\2", text)

	return " ".join(text.replace(asterisk_marker, "*").split())


def _selector(text: str) -> str:
	"""Return the GitHub-style anchor for heading text: lower case, punctuation removed, and spaces
	replaced with hyphens."""
	text = re.sub(r"[^\w\- ]", "", text.lower())
	return text.replace(" ", "-")
