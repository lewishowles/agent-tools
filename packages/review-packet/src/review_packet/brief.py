"""Render the draft brief with stable agent section markers.

Agent sections begin with ``<!-- agent:start NAME -->`` and end with
``<!-- agent:end NAME -->``. Later commands can locate these exact markers.
"""

from review_packet.progress_records import Chunk, RelatedTasks
from review_packet.snapshot import NO_COMMITS, Change, Snapshot

# The sections an agent fills in, as marker names and their headings.
AGENT_SECTIONS = (
	("review-question", "Main review question"),
	("safe-summary", "Summary without client details"),
	("known-limits", "Known limits"),
	("reviewer-verification", "What the reviewer can and cannot verify"),
)

# The text left in an agent section that still needs writing.
PLACEHOLDER = "Write this section."

# The words in a recorded review question that start the outside reviewer's part.
OUTSIDE_REVIEW_MARKER = "Outside review:"


def render(
	chunk: Chunk,
	snapshot: Snapshot,
	contents: tuple[str, ...],
	settings_name: str | None,
	related: RelatedTasks,
) -> str:
	"""Build the recorded context and the agent sections as Markdown.

	Args:
		chunk: The reviewed chunk and its task.
		snapshot: The current Git changes.
		contents: The files selected for the packet.
		settings_name: The settings file name to show, or None.
		related: Titles of prerequisite and dependent tasks.

	Returns:
		The brief. The review question section holds the chunk's recorded
		question when it has one; other agent sections hold a placeholder.
	"""
	# The task the chunk belongs to.
	task = chunk.task
	# The task's other chunks.
	siblings = [item for item in task.chunks if item.id != chunk.id]

	# The tool-written part of the brief.
	lines = [
		"# Outside review brief",
		"",
		"## Recorded context",
		"",
		f"Task: {task.title}",
		"",
		task.overview,
		"",
		f"Chunk: {chunk.title}",
		"",
		chunk.description,
		"",
		f"Review question: {chunk.review_question or '(none)'}",
		f"Settings: {settings_name or '(none)'}",
		"",
		"### Done when",
		"",
		*[f"- {item}" for item in task.done_when],
		"",
		"### Other chunks",
		"",
		*([f"- {item.title} ({item.status})" for item in siblings] or ["None."]),
		"",
		"### Depends on",
		"",
		*([f"- {title}" for title in related.prerequisites] or ["None."]),
		"",
		"### Needed by",
		"",
		*([f"- {title}" for title in related.dependents] or ["None."]),
		"",
		"### Git snapshot",
		"",
		f"Base: {snapshot.base}",
		f"Base commit: {snapshot.base_sha or NO_COMMITS}",
		f"Uncommitted changes: {'yes' if snapshot.uncommitted else 'no'}",
		"",
		"Changed paths:",
		"",
		*[_change_line(change) for change in snapshot.changes],
		"",
		"### Packet contents",
		"",
		*[f"- {path}" for path in contents],
		"",
		"### Callers",
		"",
		"Not listed yet.",
		"",
		"### Left out",
		"",
		"Not listed yet.",
		"",
	]

	# The question shown to the outside reviewer, or the placeholder when none is recorded.
	main_question = _main_question(chunk.review_question) or PLACEHOLDER

	for name, heading in AGENT_SECTIONS:
		# Use the recorded question here and "Write this section." in the other agent sections.
		body = main_question if name == "review-question" else PLACEHOLDER
		lines.extend(_agent_section(name, heading, body))

	return "\n".join(lines)


def _change_line(change: Change) -> str:
	"""Describe one changed path as a list item.

	Args:
		change: One changed path.

	Returns:
		The path and status, with the old path when the file was renamed.
	"""
	# The rename source, when there is one.
	origin = f", from {change.old_path}" if change.old_path else ""

	return f"- {change.path} ({change.status}{origin})"


def _agent_section(name: str, heading: str, body: str) -> list[str]:
	"""Write one agent section between its start and end markers.

	Args:
		name: The marker name.
		heading: The section heading.
		body: The text to put in the section.

	Returns:
		The section lines, ending with a blank line.
	"""
	return [
		f"<!-- agent:start {name} -->",
		f"## {heading}",
		"",
		body,
		f"<!-- agent:end {name} -->",
		"",
	]


def _main_question(review_question: str) -> str:
	"""Pick the part of a recorded review question meant for the outside reviewer.

	Args:
		review_question: The chunk's recorded question, which may hold the
			team's question followed by "Outside review:" and the outside
			reviewer's question.

	Returns:
		The text after "Outside review:" when the marker is present,
		otherwise the whole question, trimmed. Empty when nothing is recorded.
	"""
	# The text before the marker, the marker itself or "", and the text after it.
	_before, marker, after = review_question.partition(OUTSIDE_REVIEW_MARKER)

	return (after if marker else review_question).strip()
