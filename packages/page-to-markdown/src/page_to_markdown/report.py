"""Confidence and metadata reporting for page-to-markdown extraction."""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from page_to_markdown.select import (
	STRIP_TAGS,
	_DomBuilder,
	_DomNode,
	_find_all,
	_find_first,
)

# Heading elements used to identify the selected content root.
HEADING_TAGS = frozenset(["h1", "h2", "h3", "h4", "h5", "h6"])
# Output length below which the report lowers confidence.
SHORT_OUTPUT_CHARS = 40
# Script count that indicates a likely client-rendered app shell.
SCRIPT_TAG_MINIMUM = 3
# Link count required before content can be navigation-heavy.
NAVIGATION_LINK_MINIMUM = 3
# Share of selected text that links may contain before navigation is suspected.
NAVIGATION_TEXT_RATIO = 0.6
# Paragraph text length below which navigation is suspected.
NAVIGATION_PARAGRAPH_CHARS = 80
# Whitespace pattern used to normalise report text measurements.
WHITESPACE_PATTERN = re.compile(r"\s+")


class _CountingDomBuilder(_DomBuilder):
	"""Build the shared lightweight DOM while counting source elements."""

	def __init__(self):
		"""Initialise the shared DOM builder and its element counter."""
		super().__init__()
		# Element counts used to assess removed content and confidence.
		self.tag_counts = Counter()

	def handle_starttag(self, tag, attrs):
		"""Count a start tag before applying the shared builder's DOM handling."""
		self.tag_counts[tag.lower()] += 1
		super().handle_starttag(tag, attrs)

	def handle_startendtag(self, tag, attrs):
		"""Count a self-closing tag before applying the shared builder's DOM handling."""
		self.tag_counts[tag.lower()] += 1
		super().handle_startendtag(tag, attrs)


@dataclass
class ConfidenceReport:
	"""Structured confidence report fields, for direct rendering or display."""

	# Original URL or source label shown in the report.
	source: str
	# Label for the selected content root.
	selected_content_root: str
	# Per-element counts for content removed during selection.
	removed_elements_summary: str
	# Total number of elements removed during selection.
	removed_elements_total: int
	# Number of links in the selected content.
	links: int
	# Number of code blocks in the selected content.
	code_blocks: int
	# Overall confidence classification for the extraction.
	verdict: str
	# Explanations for any reduced-confidence classification.
	reasons: list[str] = field(default_factory=list)

	def __str__(self) -> str:
		"""Render the report as line-oriented text."""
		lines = [
			f"source: {self.source}",
			f"selected-content-root: {self.selected_content_root}",
			f"removed-elements: {self.removed_elements_summary} (total={self.removed_elements_total})",
			f"links: {self.links}",
			f"code-blocks: {self.code_blocks}",
			f"verdict: {self.verdict}",
		]
		lines.extend(f"reason: {reason}" for reason in self.reasons)
		return "\n".join(lines)


def build_report(
	raw_html: str,
	selected_html: str,
	markdown: str,
	source: str | None,
) -> ConfidenceReport:
	"""Build an advisory confidence report from raw and selected HTML."""
	raw_builder = _parse(raw_html)
	selected_builder = _parse(selected_html)
	selected_root = _content_root_label(raw_builder.root)
	removed_elements = _removed_elements(
		raw_builder.tag_counts, selected_builder.tag_counts
	)
	selected_text = _normalise_text(selected_builder.root.full_text)
	links = _find_all(selected_builder.root, lambda node: node.tag == "a")
	link_text_length = sum(len(_normalise_text(node.full_text)) for node in links)
	paragraph_text_length = sum(
		len(_normalise_text(node.full_text))
		for node in _find_all(selected_builder.root, lambda item: item.tag == "p")
	)

	reasons = []
	if _is_js_app_shell(raw_builder):
		reasons.append(
			"JS app shell detected: the body is nearly empty despite multiple "
			"script tags; use web-audit render before converting"
		)
	if not raw_builder.tag_counts:
		reasons.append("non-HTML response detected: no HTML elements were found")
	if len(markdown.strip()) < SHORT_OUTPUT_CHARS:
		reasons.append(
			"converted Markdown is empty or very short "
			f"({len(markdown.strip())} characters)"
		)
	if _is_mostly_navigation(
		len(selected_text), len(links), link_text_length, paragraph_text_length
	):
		reasons.append(
			"mostly-navigation content detected: links contain most of the "
			"selected text and there is little paragraph prose"
		)

	if reasons:
		verdict = "low-confidence"
	elif len(markdown.strip()) < 120:
		verdict = "medium-confidence"
	else:
		verdict = "high-confidence"

	removed_total = sum(removed_elements.values())
	removed_summary = ", ".join(
		f"{tag}={count}" for tag, count in sorted(removed_elements.items())
	)
	if not removed_summary:
		removed_summary = "none"

	return ConfidenceReport(
		source=source or "stdin",
		selected_content_root=selected_root,
		removed_elements_summary=removed_summary,
		removed_elements_total=removed_total,
		links=len(links),
		code_blocks=selected_builder.tag_counts.get("pre", 0),
		verdict=verdict,
		reasons=reasons,
	)


def build_metadata(
	raw_html: str,
	selected_html: str,
	source: str | None,
) -> dict[str, str | None]:
	"""Build JSON-serialisable metadata for an extracted document."""
	return {
		"title": _extract_title(raw_html, selected_html),
		"url": source if _is_http_source(source) else None,
		"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
	}


def _parse(html: str) -> _CountingDomBuilder:
	"""Parse HTML using the selector's shared DOM implementation."""
	builder = _CountingDomBuilder()
	builder.feed(html if isinstance(html, str) else "")
	builder.close()
	return builder


def _content_root_label(root: _DomNode) -> str:
	"""Report the semantic tier selected before the body or root fallback."""
	if _find_first(root, lambda node: node.tag == "main"):
		return "main"
	if _find_first(root, lambda node: node.tag == "article"):
		return "article"
	if _find_first(root, lambda node: node.attrs.get("role", "").lower() == "main"):
		return "role=main"
	if _find_first(root, lambda node: node.tag == "body"):
		return "body"
	return "root"


def _removed_elements(
	raw_counts: Counter,
	selected_counts: Counter,
) -> dict[str, int]:
	"""Count stripped elements that are absent from the selected HTML."""
	return {
		tag: raw_counts[tag] - selected_counts.get(tag, 0)
		for tag in STRIP_TAGS
		if raw_counts[tag] > selected_counts.get(tag, 0)
	}


def _is_js_app_shell(builder: _CountingDomBuilder) -> bool:
	"""Detect script-heavy HTML with almost no readable body text."""
	body = _find_first(builder.root, lambda node: node.tag == "body")
	body_text = body.full_text if body else builder.root.full_text
	return (
		builder.tag_counts.get("script", 0) >= SCRIPT_TAG_MINIMUM
		and len(_normalise_text(body_text)) < SHORT_OUTPUT_CHARS
	)


def _is_mostly_navigation(
	selected_text_length: int,
	link_count: int,
	link_text_length: int,
	paragraph_text_length: int,
) -> bool:
	"""Identify link-heavy selections with too little paragraph prose."""
	if link_count < NAVIGATION_LINK_MINIMUM or not selected_text_length:
		return False

	# Three links, 60% link text, and fewer than 80 prose characters catches
	# navigation blocks without penalising normal short articles.
	return (
		link_text_length / selected_text_length >= NAVIGATION_TEXT_RATIO
		and paragraph_text_length < NAVIGATION_PARAGRAPH_CHARS
	)


def _extract_title(raw_html: str, selected_html: str) -> str | None:
	"""Extract the raw title, falling back to the first selected heading."""
	raw_root = _parse(raw_html).root
	title = _find_first(raw_root, lambda node: node.tag == "title")
	title_text = _normalise_text(title.full_text) if title else ""
	if title_text:
		return title_text

	selected_root = _parse(selected_html).root
	heading = _find_first(selected_root, lambda node: node.tag in HEADING_TAGS)
	heading_text = _normalise_text(heading.full_text) if heading else ""
	return heading_text or None


def _normalise_text(text: str) -> str:
	"""Collapse HTML whitespace for report measurements and metadata."""
	return WHITESPACE_PATTERN.sub(" ", text).strip()


def _is_http_source(source: str | None) -> bool:
	"""Return whether a source is an HTTP(S) URL."""
	return bool(source and source.startswith(("http://", "https://")))
