"""Remove page chrome identified by class and ID words from selected content."""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from page_to_markdown.select import _DomNode

# Whole words that commonly label page furniture.
CHROME_WORDS = frozenset(
	{
		"cookie",
		"consent",
		"gdpr",
		"newsletter",
		"subscribe",
		"signup",
		"share",
		"sharing",
		"social",
		"related",
		"recommended",
		"promo",
		"sponsor",
		"advert",
		"ad",
		"ads",
		"popup",
		"modal",
	}
)

# Whole words that identify content even when a chrome word is present.
CONTENT_WORDS = frozenset({"content", "article", "post", "entry", "body"})

# Tags kept even when their class or ID contains a chrome word, along with any
# element that contains one.
PROTECTED_TAGS = frozenset(
	{"main", "article", "body", "h1", "h2", "h3", "h4", "h5", "h6"}
)

# Class and ID words are separated by HTML whitespace, hyphens, or underscores.
WORD_SEPARATOR = re.compile(r"[\s_-]+")


def prune_chrome(region: "_DomNode") -> list["_DomNode"]:
	"""Remove page chrome inside the selected region and return what was removed.

	Args:
		region: The selected DOM region, before HTML serialisation.

	Returns:
		The roots of removed subtrees in document order. The region itself is kept.
	"""
	# Measure against the original region so sibling removals cannot change the guard.
	region_text_length = region.text_length

	# The removed elements, filled in document order as the region is searched.
	removed: list[_DomNode] = []

	_prune_children(region, region_text_length, removed)
	return removed


def _prune_children(
	node: "_DomNode", region_text_length: int, removed: list["_DomNode"]
) -> None:
	"""Remove the chrome children of a node and search inside the children that stay.

	Args:
		node: The parent whose children are checked.
		region_text_length: The original selected region's non-whitespace text length.
		removed: The list of removed subtree roots to extend.
	"""
	for child in node.children[:]:
		if child.tag == "#text":
			continue
		if _should_remove(child, region_text_length):
			node.children.remove(child)
			removed.append(child)
		else:
			_prune_children(child, region_text_length, removed)


def _should_remove(node: "_DomNode", region_text_length: int) -> bool:
	"""Decide whether an element is page chrome that can go with everything inside it.

	An element goes when its class or ID has a chrome word, it has no content
	word, it holds no protected tag, and it has at most a quarter of the
	region's text.

	Args:
		node: The element being considered for removal.
		region_text_length: The original selected region's non-whitespace text length.

	Returns:
		Whether the element and its descendants can be removed.
	"""
	# The class and ID words that decide whether the element is chrome.
	words = _class_id_words(node)
	return (
		bool(words & CHROME_WORDS)
		and not words & CONTENT_WORDS
		and not _contains_protected_tag(node)
		and node.text_length * 4 <= region_text_length
	)


def _class_id_words(node: "_DomNode") -> set[str]:
	"""Read whole words from an element's class and ID attributes.

	Args:
		node: The element whose attributes are examined.

	Returns:
		Lowercase words separated by whitespace, hyphens, or underscores.
	"""
	# The class and ID are read together, so a content word in either keeps the element.
	labels = f"{node.attrs.get('class', '')} {node.attrs.get('id', '')}"
	return set(WORD_SEPARATOR.split(labels.casefold()))


def _contains_protected_tag(node: "_DomNode") -> bool:
	"""Check whether an element is, or contains, a semantic content tag or heading.

	Args:
		node: The element whose subtree is examined.

	Returns:
		Whether the node or any descendant has a protected tag.
	"""
	return node.tag in PROTECTED_TAGS or any(
		_contains_protected_tag(child)
		for child in node.children
		if child.tag != "#text"
	)
