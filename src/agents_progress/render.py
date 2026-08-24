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


# Column width task get wraps each field row and its divider to.
_TASK_GET_ROW_WRAP_WIDTH = 72

# cli-style rows place two spaces between the label and value columns.
_TASK_GET_ROW_SEPARATOR_WIDTH = 2

# Match ANSI control sequences so row labels can be found in styled output.
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
}

# Maps a _STATUS_RESULT_TYPES result type to the span() tone that renders it.
_STATUS_TONES = {
	"failed": "danger",
	"info": "info",
	"skipped": "muted",
	"success": "success",
	"warning": "warning",
}


# Keys whose values _render_object wraps at 72 columns via row_group, per command.
_OBJECT_ROW_GROUP_FIELDS = {
	"chunk get": {"description"},
}

# Commands whose row-grouped fields omit blank values.
_DROP_BLANK_ROW_GROUPS = frozenset({"task get"})

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

# Canonical field order for task get output.
_TASK_GET_FIELD_ORDER = (
	"id",
	"slug",
	"title",
	"project_id",
	"release_id",
	"overview",
	"purpose",
	"contract",
	"files",
	"acceptance_criteria",
	"verification",
	"risks",
	"status",
	"status_reason",
	"position",
	"created_at",
	"started_at",
	"completed_at",
	"updated_at",
)


def render(command: str, data: object) -> str:
	"""Render one command's stable data for a person at a terminal."""
	if command == "commands":
		return _render_commands(data)
	if command in {"next", "current"}:
		return _render_next(data)
	if command == "doctor":
		return _render_doctor(data)
	if command == "task clean":
		return _render_task_clean(data)
	if isinstance(data, dict) and "items" in data:
		return _render_list(command, data)
	if isinstance(data, dict):
		return _render_object(command, data)

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


def _render_next(data: object) -> str:
	"""Render the selected task and its active chunk with cli-style primitives."""
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
		task_status = task.get("status", "")
		task_rows = [
			{
				"label": "Task",
				"value": _render_next_position_line(
					"task",
					task_status,
					data.get("task_rank", ""),
					data.get("task_total", ""),
					"release",
				),
			},
			{
				"label": "Info",
				"value": render_span(
					f"progress task get {task.get('id', '')}",
					"muted",
					weight="normal",
				),
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

		dependency_ids = data.get("dependency_ids")
		if isinstance(dependency_ids, list) and dependency_ids:
			task_rows.append(
				{
					"label": "Dependency IDs",
					"value": render_span(
						", ".join(str(item) for item in dependency_ids),
						"muted",
						weight="normal",
					),
				}
			)

		blocks.append(render_row_group(task_rows))
		blocks.append(render_span(str(task.get("title", "")), "text", weight="bold"))
		task_overview = task.get("overview") or task.get("purpose")
		if task_overview:
			blocks.append(
				render_span(
					textwrap.fill(str(task_overview), _TASK_GET_ROW_WRAP_WIDTH),
					"muted",
					weight="normal",
				)
			)

		if isinstance(chunk, dict):
			chunk_status = chunk.get("status", "")
			blocks.append(render_divider(divider_colour="muted"))
			blocks.append(
				render_row_group(
					[
						{
							"label": "Chunk",
							"value": _render_next_position_line(
								"chunk",
								chunk_status,
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
						textwrap.fill(str(chunk_description), _TASK_GET_ROW_WRAP_WIDTH),
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
	"""Render one bounded page with cli-style rows, a per-chunk get hint, and a pagination hint."""
	if command == "task list":
		return _render_task_list(data)

	items = data.get("items", [])
	labels = {
		"release list": "Releases",
		"chunk list": "Chunks",
	}
	title = labels.get(command, "Results")
	blocks = [render_span(title)]
	for item in items:
		if not isinstance(item, dict):
			continue
		name = item.get("title") or item.get("id") or "item"
		status = item.get("status")
		identifier = str(item.get("id", ""))
		value = f"{status} ({identifier})" if status else f"({identifier})"
		result_type = _status_result_type(status) if status else ""
		item_block = render_row(
			str(name),
			value,
			result_type,
		)
		if command == "chunk list" and status and result_type != "success":
			item_block = "\n".join(
				[
					item_block,
					render_hint(f"progress chunk get {identifier}"),
				]
			)
		description = item.get("description")
		if command == "chunk list" and description:
			item_block = "\n".join(
				[
					item_block,
					render_row_group(
						[
							{
								"label": "Description",
								"value": str(description),
							}
						]
					),
				]
			)
		blocks.append(item_block)

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(render_hint(f"More results: use --offset {next_offset}."))

	return "\n\n".join(blocks)


def _render_task_list(data: dict[str, object]) -> str:
	"""Render task rows grouped by release with status and the next available action."""
	get_command = render_span("progress task get TASK_ID", weight="bold")
	move_command = render_span(
		"progress task move TASK_ID --before/--after TASK_ID", weight="bold"
	)
	action_message = f"View a task with {get_command}; reorder with {move_command}."
	blocks = [render_labelled_line("Next action", action_message)]
	columns = [
		{"key": "title", "label": "Title"},
		{"key": "status", "label": "Status"},
		{"key": "id", "label": "ID"},
	]
	for release_title, items in _group_task_items(data.get("items")):
		blocks.append(render_span(release_title, "muted", weight="normal"))
		blocks.append(
			render_table(columns, [_render_task_item(item) for item in items])
		)

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(render_hint(f"More results: use --offset {next_offset}."))

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
	"""Build one task row for the task-list table, flagging blocked titles with a warning marker."""
	title = str(item.get("title") or item.get("id") or "item")
	status = str(item.get("status", ""))
	identifier = str(item.get("id", ""))
	if status == "blocked":
		title = f"{render_span('!', 'warning', weight='normal')} {title}"

	return {
		"title": title,
		"status": render_status(_status_result_type(status), status),
		"id": render_span(f"({identifier})", "muted", weight="normal"),
	}


def _status_result_type(status: object) -> str:
	"""Map a progress status to the cli-style result type for its tone."""
	return _STATUS_RESULT_TYPES.get(str(status), "info")


def _render_object(command: str, data: dict[str, object]) -> str:
	"""Render one stable public object as labelled rows."""
	lines = []
	grouped_rows = []
	row_group_fields = (
		set(data)
		if command in _DROP_BLANK_ROW_GROUPS
		else _OBJECT_ROW_GROUP_FIELDS.get(command, set())
	)
	field_order = _TASK_GET_FIELD_ORDER if command in _DROP_BLANK_ROW_GROUPS else data
	for key in field_order:
		if key not in data:
			continue

		value = data[key]
		if key in row_group_fields:
			if command in _DROP_BLANK_ROW_GROUPS and (
				value is None or (isinstance(value, str) and not value.strip())
			):
				continue
			grouped_rows.append(
				{
					"label": _format_label(key),
					"value": "" if value is None else str(value),
				}
			)
			continue

		if grouped_rows:
			lines.append(render_row_group(grouped_rows))
			grouped_rows = []

		if key == "demoted_task":
			lines.append(f"Demoted task: {_format_demoted_task(value)}")
			continue
		if key == "unblocked_tasks":
			lines.extend(_format_unblocked_tasks(value))
			continue

		lines.append(f"{_format_label(key)}: {'' if value is None else value}")

	if grouped_rows:
		if command == "task get":
			lines.append(_render_task_get_row_group(grouped_rows))
		else:
			lines.append(render_row_group(grouped_rows))

	if command == "task get":
		return "\n" + "\n".join(lines) + "\n\n"

	return "\n".join(lines) + "\n"


def _render_task_get_row_group(rows: list[dict[str, str]]) -> str:
	"""Render task fields as aligned rows separated by border-coloured dividers."""
	label_width = max(len(row["label"]) for row in rows)
	rendered_rows = render_row_group(rows)
	row_divider = render_divider(
		divider_width=(
			label_width + _TASK_GET_ROW_SEPARATOR_WIDTH + _TASK_GET_ROW_WRAP_WIDTH
		),
		divider_colour="border",
	)
	row_blocks = _split_task_get_row_group(rendered_rows, rows, label_width)
	return f"\n{row_divider}\n".join(row_blocks)


def _split_task_get_row_group(
	rendered_rows: str,
	rows: list[dict[str, str]],
	label_width: int,
) -> list[str]:
	"""Split one styled row group while keeping wrapped value lines with their row."""
	rendered_lines = rendered_rows.splitlines()
	row_labels = [row["label"].ljust(label_width) for row in rows]
	row_blocks = []
	current_block = []
	next_label_index = 0

	for line in rendered_lines:
		plain_line = _ANSI_ESCAPE_PATTERN.sub("", line)
		if next_label_index < len(row_labels) and plain_line.startswith(
			row_labels[next_label_index]
		):
			if current_block:
				row_blocks.append("\n".join(current_block))
			current_block = [line]
			next_label_index += 1
			continue

		if not current_block:
			raise ValueError(
				"cli-style row-group output did not start with a row label"
			)

		current_block.append(line)

	if current_block:
		row_blocks.append("\n".join(current_block))

	if len(row_blocks) != len(rows):
		raise ValueError("cli-style row-group output did not contain every row")

	return row_blocks


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


def _format_unblocked_tasks(value: object) -> list[str]:
	"""Name tasks made ready when a completed task removed their last dependency, or 'none' when nothing was."""
	if not isinstance(value, list) or not value:
		return ["Unblocked tasks: none"]

	lines = ["Unblocked tasks:"]
	for task in value:
		if not isinstance(task, dict):
			continue
		lines.append(f"- {task.get('title', '')} ({task.get('id', '')})")

	return lines
