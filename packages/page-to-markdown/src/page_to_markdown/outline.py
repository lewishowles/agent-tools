"""Find the headings in converted Markdown so an agent can pick one by selector."""

import re
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Heading:
	"""Describe one heading in the converted Markdown and the selector that picks it."""

	# The heading level, from 1 for "#" to 6 for "######".
	level: int
	# The heading text as a reader sees it, without link syntax, backticks, or paired "*" emphasis.
	text: str
	# The GitHub-style anchor for the heading, unique within the document.
	selector: str


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

	for line in markdown.splitlines():
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
			)
		)

	return headings


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
