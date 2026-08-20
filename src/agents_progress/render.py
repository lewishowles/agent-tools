"""Human-readable rendering for progress read responses."""

from .style import (
	hint as render_hint,
	row as render_row,
	row_group as render_row_group,
	span as render_span,
	status as render_status,
	table as render_table,
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


def render(command: str, data: object) -> str:
	"""Render one command's stable data for a person at a terminal."""
	if command in {"next", "current"}:
		return _render_next(data)
	if command == "doctor":
		return _render_doctor(data)
	if isinstance(data, dict) and "items" in data:
		return _render_list(command, data)
	if isinstance(data, dict):
		return _render_object(data)

	return str(data)


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


def _render_next(data: object) -> str:
	"""Render the selected task and its active chunk with cli-style primitives."""
	if not isinstance(data, dict):
		return str(data)

	project = data.get("project")
	project_name = project.get("name", "") if isinstance(project, dict) else ""
	blocks = [render_row("Project", str(project_name))]
	task = data.get("task")
	chunk = data.get("chunk")
	if not isinstance(task, dict):
		blocks.append(render_row("Task", "No task is selected."))
	else:
		task_status = task.get("status", "")
		task_rows = [
			{"label": "Task", "value": str(task.get("title", ""))},
			{
				"label": "Overview",
				"value": str(task.get("overview") or task.get("purpose") or ""),
			},
			{"label": "ID", "value": str(task.get("id", ""))},
		]
		status_reason = task.get("status_reason")
		if status_reason:
			task_rows.append({"label": "Blocking reason", "value": str(status_reason)})

		dependency_ids = data.get("dependency_ids")
		if isinstance(dependency_ids, list) and dependency_ids:
			task_rows.append(
				{
					"label": "Dependency IDs",
					"value": ", ".join(str(item) for item in dependency_ids),
				}
			)

		blocks.extend(
			[
				render_row_group(task_rows),
				render_status(
					_status_result_type(task_status),
					"Task status",
					str(task_status),
				),
			]
		)
		if isinstance(chunk, dict):
			chunk_status = chunk.get("status", "")
			blocks.extend(
				[
					render_row_group(
						[
							{"label": "Chunk", "value": str(chunk.get("title", ""))},
							{
								"label": "Description",
								"value": str(chunk.get("description", "")),
							},
							{"label": "ID", "value": str(chunk.get("id", ""))},
						]
					),
					render_status(
						_status_result_type(chunk_status),
						"Chunk status",
						str(chunk_status),
					),
				]
			)

	hint_command = data.get("hint_command")
	if hint_command:
		blocks.append(render_hint(str(hint_command)))

	return "\n\n".join(blocks)


def _render_list(command: str, data: dict[str, object]) -> str:
	"""Render one bounded page with cli-style rows and a pagination hint."""
	if command == "task list":
		return _render_task_list(data)

	items = data.get("items", [])
	labels = {
		"release list": "Releases",
		"chunk list": "Chunks",
		"ready": "Ready tasks",
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
		blocks.append(
			render_row(
				str(name),
				value,
				_status_result_type(status) if status else "",
			)
		)

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		blocks.append(render_hint(f"More results: use --offset {next_offset}."))

	return "\n\n".join(blocks)


def _render_task_list(data: dict[str, object]) -> str:
	"""Render task rows grouped by release with status and follow-up hints."""
	get_command = render_span("progress task get TASK_ID", weight="bold")
	move_command = render_span(
		"progress task move TASK_ID --before/--after TASK_ID", weight="bold"
	)
	hint_message = f"View a task with {get_command}; reorder with {move_command}."
	blocks = [render_hint(hint_message)]
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


def _render_object(data: dict[str, object]) -> str:
	"""Render one stable public object as labelled rows."""
	lines = []
	for key, value in data.items():
		if key == "demoted_task":
			lines.append(f"Demoted task: {_format_demoted_task(value)}")
			continue
		if key == "unblocked_tasks":
			lines.extend(_format_unblocked_tasks(value))
			continue

		label = key.replace("_", " ").capitalize()
		if label == "Id" or label.endswith(" id"):
			label = label[:-2] + "ID"
		lines.append(f"{label}: {'' if value is None else value}")

	return "\n".join(lines) + "\n"


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
