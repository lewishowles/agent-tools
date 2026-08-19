"""Human-readable rendering for progress read responses."""


def render(command: str, data: object) -> str:
	"""Render one command's stable data for a person at a terminal."""
	if command in {"next", "current"}:
		return _render_next(data)
	if isinstance(data, dict) and "items" in data:
		return _render_list(command, data)
	if isinstance(data, dict):
		return _render_object(data)

	return str(data)


def _render_next(data: object) -> str:
	"""Render the selected task and its active chunk as prose lines."""
	if not isinstance(data, dict):
		return str(data)

	task = data.get("task")
	chunk = data.get("chunk")
	lines = [f"Project: {data.get('project', {}).get('name', '')}", ""]
	if not isinstance(task, dict):
		lines.extend(["No task is selected.", ""])
	else:
		lines.extend(
			[
				f"Task: {task.get('title', '')}",
				f"Status: {task.get('status', '')}",
				f"Overview: {task.get('overview') or task.get('purpose') or ''}",
				f"ID: {task.get('id', '')}",
			]
		)
		status_reason = task.get("status_reason")
		if status_reason:
			lines.append(f"Blocking reason: {status_reason}")

		dependency_ids = data.get("dependency_ids")
		if isinstance(dependency_ids, list) and dependency_ids:
			lines.append(
				f"Dependency IDs: {', '.join(str(item) for item in dependency_ids)}"
			)

		lines.append("")
		if isinstance(chunk, dict):
			lines.extend(
				[
					f"Chunk: {chunk.get('title', '')}",
					f"Description: {chunk.get('description', '')}",
					f"Status: {chunk.get('status', '')}",
					f"ID: {chunk.get('id', '')}",
					"",
				]
			)

	hint = data.get("hint_command")
	if hint:
		lines.extend([f"Next: {hint}", ""])

	return "\n".join(lines)


def _render_list(command: str, data: dict[str, object]) -> str:
	"""Render one bounded page of records with a pagination hint when more exist."""
	items = data.get("items", [])
	labels = {
		"release list": "Releases",
		"task list": "Tasks",
		"chunk list": "Chunks",
		"ready": "Ready tasks",
	}
	title = labels.get(command, "Results")
	lines = [f"{title}:"]
	for item in items:
		if not isinstance(item, dict):
			continue
		name = item.get("title") or item.get("id") or "item"
		status = item.get("status")
		status_text = f" [{status}]" if status else ""
		lines.append(f"- {name}{status_text} ({item.get('id', '')})")

	if data.get("has_more"):
		next_offset = int(data.get("offset", 0)) + int(data.get("limit", 0))
		lines.extend(["", f"More results: use --offset {next_offset}."])

	return "\n".join(lines) + "\n"


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
