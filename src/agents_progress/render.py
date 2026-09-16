"""Human-readable rendering for progress read responses."""

import re
import textwrap

from .style import (
	divider as render_divider,
	hint as render_hint,
	labelled_line as render_labelled_line,
	row as render_row,
	row_group as render_row_group,
	span as render_span,
	status as render_status,
	table as render_table,
)


# Column width the long prose fields wrap to.
_ROW_WRAP_WIDTH = 72

# Match ANSI control sequences so styled text can be measured by its width.
_ANSI_ESCAPE_PATTERN = re.compile(
	r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)


# Maps a progress status string to the cli-style result type that colours it.
_STATUS_RESULT_TYPES = {
	"active": "info",
	"blocked": "failed",
	"complete": "success",
	"completed": "success",
	"done": "success",
	"in-progress": "info",
	"needs-decision": "warning",
	"pending": "skipped",
	"ready": "skipped",
	"skipped": "skipped",
}

# Maps a _STATUS_RESULT_TYPES result type to the span() tone that renders it.
_STATUS_TONES = {
	"failed": "danger",
	"info": "info",
	"skipped": "muted",
	"success": "success",
	"warning": "warning",
}

# Plain-text width the task-list status cell is padded to so task titles line up.
# "needs-decision" is the widest task status; the leading "! " stands in for
# cli-style's one-column marker (rendered as a warning sign).
_TASK_STATUS_COLUMN_WIDTH = len("! needs-decision")


# Explains why a non-force clean pass leaves task history in place.
_TASK_CLEAN_KEPT_HINT = (
	"Tasks with notes or dependencies are kept to avoid removing potentially "
	"relevant notes. Use --force to override."
)

# Maps dependency directions from the write-store response to display labels.
_TASK_CLEAN_DEPENDENCY_LABELS = {
	"depends_on": "Depends on:",
	"required_by": "Required by:",
}


def render(command: str, data: object) -> str:
	"""Render one command's stable data for a person at a terminal."""
	if (
		command == "project init"
		and isinstance(data, dict)
		and data.get("already_initialised")
	):
		# A repeat init reuses the project current layout, so drop the machine-readable flag first.
		project = {
			key: value for key, value in data.items() if key != "already_initialised"
		}
		return (
			render_status("info", "Repo already initialised")
			+ "\n"
			+ _render_object(project)
		)
	if command == "commands":
		return _render_commands(data)
	if command in {"next", "current"}:
		return _render_next(data)
	if command == "task get" and isinstance(data, dict):
		return _render_task(data)
	if command == "chunk get" and isinstance(data, dict):
		return _render_chunk(data)
	if command == "doctor":
		return _render_doctor(data)
	if command == "task clean":
		return _render_task_clean(data)
	if command in {"task complete", "chunk complete"} and isinstance(data, dict):
		# Completion prints one success line, not every record field; handled here
		# rather than in _render_object so a plain get still shows all fields.
		record_type = command.split()[0]  # "task" or "chunk"

		# Completion also names the parent so the next command has its ID to hand.
		# A task with no release gets no line.
		parent_key = "task_id" if command == "chunk complete" else "release_id"
		parent_id = data.get(parent_key)
		parent_line = (
			"\n"
			+ render_span(
				f"{_format_label(parent_key)}: {parent_id}",
				"muted",
				weight="normal",
			)
			if parent_id
			else ""
		)

		return (
			render_status(
				"success", f"Completed {record_type}", str(data.get("title") or "")
			)
			+ parent_line
			+ "\n"
		)
	if isinstance(data, dict) and "items" in data:
		return _render_list(command, data)
	if isinstance(data, dict):
		return _render_object(data)

	return str(data)


def _render_commands(data: object) -> str:
	"""Render the command manifest as a table of paths, descriptions, and flags."""
	if not isinstance(data, list):
		return str(data)

	rows = []
	for command in data:
		if not isinstance(command, dict):
			continue

		flags = []
		for flag in command.get("flags", []):
			if not isinstance(flag, dict):
				continue

			names = flag.get("names", [])
			if not isinstance(names, list):
				continue

			required = "required" if flag.get("required") else "optional"
			flags.append(f"{' / '.join(str(name) for name in names)} ({required})")

		rows.append(
			{
				"command": str(command.get("path", "")),
				"description": str(command.get("help", "")),
				"flags": ", ".join(flags),
			}
		)

	table = render_table(
		[
			{"key": "command", "label": "Command"},
			{"key": "description", "label": "Description"},
			{"key": "flags", "label": "Flags"},
		],
		rows,
	)
	return f"{table}\n" if table else ""


def _render_doctor(data: object) -> str:
	"""Render doctor findings or the clean result."""
	if not isinstance(data, dict):
		return str(data)

	findings = data.get("findings", [])
	if not findings:
		return "Doctor: clean\n"

	lines = ["Doctor findings:"]
	if isinstance(findings, list):
		for finding in findings:
			if not isinstance(finding, dict):
				continue
			field = finding.get("field", "field")
			name = finding.get("title") or finding.get("id") or "record"
			lines.append(f"- {field}: {name} ({finding.get('id', '')})")

	return "\n".join(lines) + "\n"


def _render_task_clean(data: object) -> str:
	"""Render task-clean counts and the tasks kept for explicit review."""
	if not isinstance(data, dict):
		return str(data)

	removed_count = data.get("removed_count", 0)
	if not isinstance(removed_count, int):
		removed_count = 0

	kept = data.get("blocked", [])
	kept_tasks = (
		[task for task in kept if isinstance(task, dict)]
		if isinstance(kept, list)
		else []
	)
	releases_removed = data.get("releases_removed", [])
	kept_count = len(kept_tasks)
	blocks = [
		render_span(
			f"{_count_label(removed_count, 'task', 'tasks')} removed",
			"success",
			weight="normal",
		),
		render_span(
			f"{_count_label(kept_count, 'task', 'tasks')} kept",
			"warning" if kept_count else "muted",
			weight="normal",
		),
		"",
	]
	if kept_tasks:
		blocks.extend(
			[
				f"{_render_task_clean_label('Hint')}  {_TASK_CLEAN_KEPT_HINT}",
				"",
			]
		)
		for index, task in enumerate(kept_tasks):
			if index:
				blocks.append("")
			blocks.extend(_render_task_clean_task(task))
		blocks.append("")

	releases_count = len(releases_removed) if isinstance(releases_removed, list) else 0
	blocks.append(
		render_span(
			f"{_count_label(releases_count, 'release', 'releases')} removed",
			"muted",
			weight="normal",
		)
	)
	if isinstance(releases_removed, list):
		for release in releases_removed:
			if isinstance(release, dict):
				blocks.append(
					f"{_render_task_clean_label('Removed release')}  "
					f"{release.get('title', '')} ({release.get('id', '')})"
				)

	return "\n".join(blocks) + "\n"


def _count_label(count: int, singular: str, plural: str) -> str:
	"""Return a count with its singular or plural label."""
	return f"{count} {singular if count == 1 else plural}"


def _render_task_clean_label(label: str) -> str:
	"""Render a task-clean label in the muted tone."""
	return render_span(label, "muted", weight="normal")


def _render_task_clean_task(task: dict[str, object]) -> list[str]:
	"""Render one kept task and the history that kept it."""
	notes = task.get("notes", [])
	note_records = (
		[note for note in notes if isinstance(note, dict)]
		if isinstance(notes, list)
		else []
	)
	dependencies = task.get("dependencies", [])
	dependency_records = (
		[dependency for dependency in dependencies if isinstance(dependency, dict)]
		if isinstance(dependencies, list)
		else []
	)
	reasons = []
	note_counts = {}
	for note in note_records:
		note_type = str(note.get("type", ""))
		note_counts[note_type] = note_counts.get(note_type, 0) + 1
	for note_type in ("discovery", "decision"):
		if note_counts.get(note_type):
			reasons.append(
				_count_label(
					note_counts[note_type],
					f"{note_type} note",
					f"{note_type} notes",
				)
			)
	if dependency_records:
		reasons.append(
			_count_label(len(dependency_records), "dependency", "dependencies")
		)

	label_width = len("Kept task")
	kept_task_label = _render_task_clean_label("Kept task".ljust(label_width))
	reason_label = _render_task_clean_label("Reason".ljust(label_width))
	blocks = [
		f"{kept_task_label}  {task.get('title', '')} ({task.get('id', '')})",
		f"{reason_label}  {', '.join(reasons)}",
		"",
	]

	for index, note in enumerate(note_records):
		if index:
			blocks.append("")
		note_type = str(note.get("type", "")).capitalize()
		blocks.extend(
			[
				_render_task_clean_label(f"{note_type} note"),
				str(note.get("body", "")),
			]
		)

	if dependency_records:
		blocks.extend(["", _render_task_clean_label("Dependency")])
		blocks.extend(
			_render_task_clean_dependency(dependency)
			for dependency in dependency_records
		)

	return blocks


def _render_task_clean_dependency(dependency: dict[str, object]) -> str:
	"""Render one dependency using its resolved direction and endpoint details."""
	direction = str(dependency.get("direction", ""))
	label = _TASK_CLEAN_DEPENDENCY_LABELS.get(direction, "Dependency:")
	return (
		f"{_render_task_clean_label(label)} "
		f"{dependency.get('other_task_title', '')} "
		f"({dependency.get('other_task_id', '')})"
	)


def _render_task(task: dict[str, object]) -> str:
	"""Render one task record in full, shared by task get and next.

	Both commands show the same task, so next carries the whole record and a
	follow-up task get returns nothing new. Slug, position and timestamps are
	left to --json.
	"""
	blocks = [render_span(str(task.get("title", "")), "text", weight="bold")]
	status = task.get("status", "")
	task_rows = [
		{
			"label": "Status",
			"value": render_span(
				str(status).replace("-", " "),
				_STATUS_TONES.get(_status_result_type(status), "info"),
				weight="bold",
			),
		},
		{
			"label": "ID",
			"value": render_span(str(task.get("id", "")), "muted", weight="normal"),
		},
	]
	status_reason = task.get("status_reason")
	if status_reason:
		task_rows.append(
			{
				"label": "Blocking reason",
				"value": render_span(str(status_reason), "muted", weight="normal"),
			}
		)
	blocks.append(render_row_group(task_rows))

	for label, key in (("Overview", "overview"), ("Purpose", "purpose")):
		value = task.get(key)
		if not value:
			continue
		blocks.extend(
			[
				render_span(label),
				render_span(
					textwrap.fill(str(value), _ROW_WRAP_WIDTH),
					"muted",
					weight="normal",
				),
			]
		)

	chunks = task.get("chunks", [])
	blocks.append(render_span("Chunks"))
	chunk_items = _render_chunk_items(chunks)
	blocks.append(
		chunk_items
		if chunk_items
		else render_span("No chunks.", "muted", weight="normal")
	)

	split_rationale = task.get("split_rationale")
	if split_rationale:
		blocks.extend(
			[
				render_span("Split rationale"),
				render_span(
					textwrap.fill(str(split_rationale), _ROW_WRAP_WIDTH),
					"muted",
					weight="normal",
				),
			]
		)

	for label, key in (("Contract", "contract"), ("Files", "files")):
		values = task.get(key)
		if not isinstance(values, list) or not values:
			continue
		blocks.append(render_span(label))
		blocks.extend(
			render_span(f"- {item}", "muted", weight="normal") for item in values
		)

	for label, key in (
		("Acceptance criteria", "acceptance_criteria"),
		("Verification", "verification"),
		("Risks", "risks"),
	):
		value = task.get(key)
		if not value:
			continue
		blocks.extend(
			[
				render_span(label),
				render_span(
					textwrap.fill(str(value), _ROW_WRAP_WIDTH),
					"muted",
					weight="normal",
				),
			]
		)

	footer_rows = []
	for label, key in (("Project ID", "project_id"), ("Release ID", "release_id")):
		value = task.get(key)
		if value is None or not str(value).strip():
			continue
		footer_rows.append(
			{
				"label": label,
				"value": render_span(str(value), "muted", weight="normal"),
			}
		)
	if footer_rows:
		blocks.extend(
			[render_divider(divider_colour="muted"), render_row_group(footer_rows)]
		)

	return "\n\n".join(blocks)


def _render_chunk(chunk: dict[str, object]) -> str:
	"""Render one chunk record in full for chunk get.

	The description is shown whole here; chunk list keeps only its first line.
	Position and timestamps are left to --json.
	"""
	blocks = [render_span(str(chunk.get("title", "")), "text", weight="bold")]
	status = chunk.get("status", "")
	chunk_rows = [
		{
			"label": "Status",
			"value": render_span(
				str(status).replace("-", " "),
				_STATUS_TONES.get(_status_result_type(status), "info"),
				weight="bold",
			),
		},
		{
			"label": "Chunk ID",
			"value": render_span(str(chunk.get("id", "")), "muted", weight="normal"),
		},
		{
			"label": "Task ID",
			"value": render_span(
				str(chunk.get("task_id", "")), "muted", weight="normal"
			),
		},
	]
	blocks.append(render_row_group(chunk_rows))

	description = chunk.get("description")
	if description:
		blocks.extend(
			[
				render_span("Description"),
				render_span(
					textwrap.fill(str(description), _ROW_WRAP_WIDTH),
					"muted",
					weight="normal",
				),
			]
		)

	return "\n\n".join(blocks)


def _render_next(data: object) -> str:
	"""Render the selected task and its active chunk.

	Adds what only next knows to the shared task view: where the task and chunk
	sit in their release, the dependency ids, and the active chunk.
	"""
	if not isinstance(data, dict):
		return str(data)

	project = data.get("project")
	project_name = project.get("name", "") if isinstance(project, dict) else ""
	blocks = [
		render_row(
			"Project",
			str(project_name),
			label_colour="muted",
			value_colour="accent",
		),
		render_divider(divider_colour="muted"),
	]
	task = data.get("task")
	chunk = data.get("chunk")
	if not isinstance(task, dict):
		blocks.append(render_row("Task", "No task is selected."))
	else:
		blocks.append(
			render_row_group(
				[
					{
						"label": "Task",
						"value": _render_next_position_line(
							"task",
							task.get("status", ""),
							data.get("task_rank", ""),
							data.get("task_total", ""),
							"release",
						),
					}
				]
			)
		)
		blocks.append(_render_task(task))

		dependency_ids = data.get("dependency_ids")
		if isinstance(dependency_ids, list) and dependency_ids:
			blocks.append(
				render_row(
					"Dependency IDs",
					", ".join(str(item) for item in dependency_ids),
				)
			)

		if isinstance(chunk, dict):
			blocks.append(render_divider(divider_colour="muted"))
			blocks.append(
				render_row_group(
					[
						{
							"label": "Chunk",
							"value": _render_next_position_line(
								"chunk",
								chunk.get("status", ""),
								data.get("chunk_rank", ""),
								data.get("chunk_total", ""),
								"task",
							),
						},
						{
							"label": "Info",
							"value": render_span(
								f"progress chunk get {chunk.get('id', '')}",
								"muted",
								weight="normal",
							),
						},
					]
				)
			)
			blocks.append(
				render_span(str(chunk.get("title", "")), "text", weight="bold")
			)
			chunk_description = chunk.get("description")
			if chunk_description:
				blocks.append(
					render_span(
						textwrap.fill(str(chunk_description), _ROW_WRAP_WIDTH),
						"muted",
						weight="normal",
					)
				)

	return "\n\n".join(blocks)


def _render_next_position_line(
	object_name: str,
	status: object,
	rank: object,
	total: object,
	parent_name: str,
) -> str:
	"""Render the status word plus the record's 1-based rank among its siblings."""
	status_label = str(status).replace("-", " ")
	position_label = f"• {object_name} {rank} / {total} for {parent_name}"
	status_tone = _STATUS_TONES.get(_status_result_type(status), "info")
	return " ".join(
		[
			render_span(status_label, status_tone, weight="bold"),
			render_span(position_label, "muted", weight="normal"),
		]
	)


def _render_list(command: str, data: dict[str, object]) -> str:
	"""Render one bounded page with cli-style rows and a pagination hint."""
	if command == "task list":
		return _render_task_list(data)
	if command == "chunk list":
		return _render_chunk_list(data)
	if command == "search":
		return _render_search(data)
	if command in {"discovery list", "decision list"}:
		return _render_note_list(command, data)

	items = data.get("items", [])
	labels = {
		"release list": "Releases",
	}
	title = labels.get(command, "Results")
	blocks = [render_span(title)]
	for item in items:
		if not isinstance(item, dict):
			continue
		name = item.get("title") or item.get("id") or "item"
		status = item.get("status")
		identifier = str(item.get("id", ""))
		value = f"{status} {identifier}" if status else identifier
		result_type = _status_result_type(status) if status else ""
		item_block = render_row(
			str(name),
			value,
			result_type,
		)
		blocks.append(item_block)

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(render_hint(f"More results: use --offset {next_offset}."))

	if command == "release list":
		hidden_done_count = data.get("hidden_done_count", 0)
		if hidden_done_count:
			release_label = "release" if hidden_done_count == 1 else "releases"
			blocks.append(
				render_hint(
					f"{hidden_done_count} completed {release_label} hidden. "
					"Use --all to show them."
				)
			)

	return "\n\n".join(blocks)


def _render_note_list(command: str, data: dict[str, object]) -> str:
	"""Render note bodies with the task or release that owns each note."""
	note_label = command.split()[0].capitalize()
	items = data.get("items", [])
	note_records = (
		[item for item in items if isinstance(item, dict)]
		if isinstance(items, list)
		else []
	)
	if not note_records:
		return render_span(f"No {note_label.lower()} notes.", "muted", weight="normal")

	blocks = [render_span(f"{note_label} notes")]

	for note in note_records:
		task_id = note.get("task_id")
		release_id = note.get("release_id")
		owner_type = "task" if task_id else "release"
		owner_id = task_id or release_id or ""
		blocks.append(
			"\n".join(
				[
					str(note.get("body", "")),
					render_span(f"{owner_type} {owner_id}", "muted", weight="normal"),
				]
			)
		)

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(render_hint(f"More results: use --offset {next_offset}."))

	return "\n\n".join(blocks)


def _render_search(data: dict[str, object]) -> str:
	"""Render search results: an empty-state line, or one block per hit followed by the paging hint and next-action line.

	Expects the search term under ``data["term"]``; the CLI adds it for human output only.
	"""
	items = data.get("items", [])
	if not isinstance(items, list):
		return render_span("No matches.", "muted", weight="normal")

	term = str(data.get("term", ""))
	blocks = [
		_render_search_item(item, term) for item in items if isinstance(item, dict)
	]

	if not blocks:
		return render_span("No matches.", "muted", weight="normal")

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(render_hint(f"More results: use --offset {next_offset}."))

	task_get = render_span("progress task get TASK_ID", weight="bold")
	chunk_get = render_span("progress chunk get CHUNK_ID", weight="bold")
	blocks.append(
		render_labelled_line(
			"Next action",
			f"View a task with {task_get} or a chunk with {chunk_get}.",
		)
	)

	return "\n\n".join(blocks)


def _render_search_item(item: dict[str, object], term: str) -> str:
	"""Render one search hit: a heading line naming the record, then one indented line per matched prose field with the term in bold.

	Chunk hits also name their parent task so a reader can tell which task the chunk belongs to.
	"""
	record_type = str(item.get("type", "result")).capitalize()
	status_value = str(item.get("status", ""))
	status_label = status_value.replace("-", " ")
	title = str(item.get("title") or item.get("id") or "item")
	identifier = str(item.get("id", ""))
	heading_parts = [
		render_span(record_type, "muted", weight="normal"),
		render_span(
			status_label,
			_STATUS_TONES.get(_status_result_type(status_value), "info"),
			weight="bold",
		),
		render_span(title, "text", weight="bold"),
		render_span(identifier, "muted", weight="normal"),
	]
	if item.get("type") == "chunk" and item.get("task_title"):
		heading_parts.append(
			render_span(f"parent: {item['task_title']}", "muted", weight="normal")
		)

	lines = [" · ".join(heading_parts)]
	snippets = item.get("snippets", {})
	if isinstance(snippets, dict):
		for field, value in snippets.items():
			values = value if isinstance(value, list) else [value]
			for snippet in values:
				lines.append(f"  {field}: {_highlight_term(str(snippet), term)}")

	return "\n".join(lines)


def _highlight_term(text: str, term: str) -> str:
	"""Wrap every case-insensitive occurrence of the term in bold, leaving the text unchanged when the term is empty or absent."""
	if not term:
		return text

	matches = list(re.finditer(re.escape(term), text, re.IGNORECASE))
	if not matches:
		return text

	parts = []
	last_end = 0
	for match in matches:
		parts.append(text[last_end : match.start()])
		parts.append(render_span(match.group(0), weight="bold"))
		last_end = match.end()
	parts.append(text[last_end:])

	return "".join(parts)


def _render_chunk_list(data: dict[str, object]) -> str:
	"""Render chunk rows under a heading, opening with the task's title and ID.

	The CLI adds the task for text output. Without it, the list starts at the heading.
	"""
	# The task these chunks belong to, added by the CLI for text output.
	task = data.get("task")
	# Sections of the output, separated by blank lines.
	blocks = []
	if isinstance(task, dict):
		blocks.append(
			" ".join(
				[
					render_span(str(task.get("title", "")), weight="normal"),
					render_span("·", "muted", weight="normal"),
					render_span(str(task.get("id", "")), "muted", weight="normal"),
				]
			)
		)

	blocks.append(render_span("Chunks"))
	chunk_items = _render_chunk_items(data.get("items", []))
	if chunk_items:
		blocks.append(chunk_items)
	else:
		blocks.append(render_span("No chunks.", "muted", weight="normal"))

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(
			render_span(
				f"More results: use --offset {next_offset}.",
				"muted",
				weight="normal",
			)
		)

	return "\n\n".join(blocks)


def _render_chunk_items(items: object) -> str:
	"""Render valid chunk items with a blank line between each row."""
	if not isinstance(items, list):
		return ""

	blocks: list[str] = []
	for item in items:
		if not isinstance(item, dict):
			continue

		blocks.append(_render_chunk_item(item))

	if not blocks:
		return ""

	return "\n\n".join(blocks)


def _render_chunk_item(item: dict[str, object]) -> str:
	"""Render one chunk row: title, then status and ID, then the first description line for unfinished chunks.

	Only the first line of the description is kept, so a long chunk record does not
	swamp the list. Use chunk get to read the whole description.
	"""
	result_type = _status_result_type(item.get("status"))
	status_group = {
		"skipped": "pending",
		"success": "done",
	}.get(result_type, "active")
	identifier = str(item.get("id", ""))
	title = str(item.get("title") or item.get("id") or "item")
	# The title on the first line, wrapped like the description below it.
	title_block = render_span(
		textwrap.fill(title, _ROW_WRAP_WIDTH),
		"text",
		weight="normal",
	)

	if status_group == "done":
		status = render_status("success", "done")
	elif status_group == "active":
		status = render_status("info", "active")
	else:
		status = render_status("skipped", "pending")

	# The second line: status, then the muted chunk ID.
	status_line = " ".join(
		[
			status,
			render_span("·", "muted", weight="normal"),
			render_span(identifier, "muted", weight="normal"),
		]
	)
	row = f"{title_block}\n{status_line}"

	description = item.get("description")
	if status_group == "done" or not description:
		return row

	description_block = render_span(
		textwrap.fill(str(description).splitlines()[0], _ROW_WRAP_WIDTH),
		"muted",
		weight="normal",
	)
	return f"{row}\n\n{description_block}"


def _render_task_list(data: dict[str, object]) -> str:
	"""Render the task list: an empty-state line, or the release-grouped tables followed by the next-action line."""
	task_groups = _group_task_items(data.get("items"))
	if not task_groups:
		return render_span("No tasks.", "muted", weight="normal")

	get_command = render_span("progress task get TASK_ID", weight="bold")
	move_command = render_span(
		"progress task move TASK_ID --before/--after TASK_ID", weight="bold"
	)
	action_message = f"View a task with {get_command}; reorder with {move_command}."
	blocks = []
	columns = [
		{"key": "status", "label": "Status"},
		{"key": "title", "label": "Title"},
		{"key": "id", "label": "ID"},
	]
	for release_title, items in task_groups:
		blocks.append(render_span(release_title, weight="normal"))
		blocks.append(
			render_table(columns, [_render_task_item(item) for item in items])
		)

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(render_hint(f"More results: use --offset {next_offset}."))

	blocks.append(render_labelled_line("Next action", action_message))

	return "\n\n".join(blocks)


def _group_task_items(
	items: object,
) -> list[tuple[str, list[dict[str, object]]]]:
	"""Group task rows by release while retaining each row's input order."""
	groups: dict[str, tuple[str, list[dict[str, object]]]] = {}
	if not isinstance(items, list):
		return []

	for item in items:
		if not isinstance(item, dict):
			continue

		release_id = str(item.get("release_id") or "unassigned")
		release_title = str(item.get("release_title") or "Unassigned")
		if release_id not in groups:
			groups[release_id] = (release_title, [])

		groups[release_id][1].append(item)

	return list(groups.values())


def _render_task_item(item: dict[str, object]) -> dict[str, str]:
	"""Build one task-list row, padding the status cell so titles align across releases."""
	title = str(item.get("title") or item.get("id") or "item")
	status = str(item.get("status", ""))
	identifier = str(item.get("id", ""))
	rendered_status = render_status(_status_result_type(status), status)

	# Each release group is rendered as its own table, so pad every status cell to
	# one shared width here to line titles up across groups. Measure the plain
	# text because the rendered status carries ANSI colour codes.
	status_width = len(_ANSI_ESCAPE_PATTERN.sub("", rendered_status))
	padding = " " * max(0, _TASK_STATUS_COLUMN_WIDTH - status_width)

	return {
		"title": title,
		"status": f"{rendered_status}{padding}",
		"id": render_span(identifier, "muted", weight="normal"),
	}


def _status_result_type(status: object) -> str:
	"""Map a progress status to the cli-style result type for its tone."""
	return _STATUS_RESULT_TYPES.get(str(status), "info")


def _render_object(data: dict[str, object]) -> str:
	"""Render one stable public object as labelled rows."""
	lines = []
	for key in data:
		if key not in data:
			continue

		value = data[key]
		if key == "demoted_task":
			lines.append(f"Demoted task: {_format_demoted_task(value)}")
			continue

		lines.append(f"{_format_label(key)}: {'' if value is None else value}")

	return "\n".join(lines) + "\n"


def _format_label(key: str) -> str:
	"""Title-case a snake_case data key, keeping an id suffix as ID rather than Id."""
	label = key.replace("_", " ").capitalize()
	if label == "Id" or label.endswith(" id"):
		return label[:-2] + "ID"

	return label


def _format_demoted_task(value: object) -> str:
	"""Name the task task_start demoted back to ready, or blank when none was."""
	if not isinstance(value, dict):
		return ""

	return f"{value.get('title', '')} ({value.get('id', '')})"
