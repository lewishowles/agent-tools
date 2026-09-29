"""Turn a review draft into a Markdown comment packet."""

from pathlib import Path

from review_feedback.draft import Draft, DraftEntry, location_text

# Opening instructions for the agent that receives the packet.
INTRO = (
	"Please work through the feedback below. Ask any clarifying questions before making changes. "
	"When the fixes are done, show a before and after for each item, so I don't have to find it in the source again.\n\n"
)


def render_packet(draft: Draft) -> str:
	"""Build the full Markdown packet from a draft's entries, or an empty string when there are none."""
	if not draft.entries:
		return ""

	# Store one rendered section for each draft entry.
	entries = [_render_entry(entry) for entry in draft.entries]
	return INTRO + "\n\n".join(entries) + "\n"


def _render_entry(entry: DraftEntry) -> str:
	"""Format one draft entry as a numbered heading with its location and comment."""
	# Keep the persisted coordinates in the heading.
	location = location_text(entry)
	# Mark source that is no longer present at HEAD.
	removed_suffix = " (removed at HEAD)" if entry.side == "removed" else ""
	# Keep the heading format used by existing packets.
	heading = f"### {entry.number}. `{location}`{removed_suffix}"
	# Preserve the old heading and comment output when no source text is stored.
	parts = [heading]
	if entry.selection_text:
		parts.append(_render_selection(entry))
	parts.append(entry.comment)
	return "\n\n".join(parts)


def _render_selection(entry: DraftEntry) -> str:
	"""Render stored source text in a Markdown fence labelled from its path."""
	# Prevent backticks in source text from closing the block.
	fence = _fence_for(entry.selection_text)
	# Let Markdown identify the source language from its path.
	language = _language_label(entry.path)
	# Do not add an empty code line after a stored trailing line break.
	separator = "" if entry.selection_text.endswith(("\n", "\r")) else "\n"
	return f"{fence}{language}\n{entry.selection_text}{separator}{fence}"


def _language_label(path: str) -> str:
	"""Return a lower-case Markdown language label, or text for extensionless paths."""
	# Use the path extension as the Markdown language label.
	suffix = Path(path).suffix
	return suffix[1:].lower() or "text"


def _fence_for(selection_text: str) -> str:
	"""Return a backtick fence longer than every run in the stored source text."""
	# The closing delimiter must be longer than every embedded backtick run.
	longest_run = 0
	current_run = 0

	# Each source character contributes to the required fence length.
	for character in selection_text:
		if character == "`":
			current_run += 1
			longest_run = max(longest_run, current_run)
		else:
			current_run = 0

	return "`" * max(3, longest_run + 1)
