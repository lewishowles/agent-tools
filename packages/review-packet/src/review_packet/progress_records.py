"""Read the review context through the installed progress command."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# How long one progress command may run, in seconds.
PROGRESS_TIMEOUT = 10


class ProgressError(RuntimeError):
	"""The progress command is unavailable or returned an unusable record."""

	def __init__(self, message: str, code: str = "environment") -> None:
		"""Store the message and the kind of failure progress reported.

		Args:
			message: The message for the user.
			code: Progress's error kind, such as not-found, which sets the
				exit code. Environment when progress gave no kind.
		"""
		super().__init__(message)

		# Progress's error kind, which the command maps to an exit code.
		self.code = code


@dataclass(frozen=True)
class TaskChunk:
	"""One chunk in the task, including the reviewed chunk."""

	# The record identifier distinguishes this chunk from the reviewed one.
	id: str
	# The title gives the reviewer context for nearby work.
	title: str
	# The status shows how far nearby work has progressed.
	status: str


@dataclass(frozen=True)
class Task:
	"""The task details needed by a review brief."""

	# The task identifier links this record to dependency records.
	id: str
	# The task title names the wider work.
	title: str
	# The overview explains the intended outcome.
	overview: str
	# The completion criteria give the reviewer the task contract.
	done_when: tuple[str, ...]
	# Every chunk in the task, including the reviewed one.
	chunks: tuple[TaskChunk, ...]


@dataclass(frozen=True)
class Chunk:
	"""The reviewed chunk and its parent task."""

	# The chunk identifier names the work under review.
	id: str
	# The title names the reviewed step.
	title: str
	# The description states the reviewed step's scope.
	description: str
	# The question focuses the reviewer, or is empty when none was recorded.
	review_question: str
	# The parent task supplies the wider contract.
	task: Task


@dataclass(frozen=True)
class RelatedTasks:
	"""The task titles linked to the reviewed task."""

	# Prerequisites are tasks the reviewed task needs first.
	prerequisites: tuple[str, ...]
	# Dependents are tasks waiting for the reviewed task.
	dependents: tuple[str, ...]


def read_chunk(root: Path, chunk_id: str) -> Chunk:
	"""Read a chunk and its parent task from progress.

	Args:
		root: The repository worktree root.
		chunk_id: The chunk to read.

	Returns:
		The chunk, with its task and the task's chunks.

	Raises:
		ProgressError: Progress cannot read the chunk, or the record is
			missing a required field.
	"""
	# The chunk record from progress.
	data = _read(root, "chunk", "get", chunk_id)

	try:
		return _parse_chunk(data)
	except (KeyError, TypeError) as error:
		raise ProgressError(
			"The progress chunk has incomplete task details."
		) from error


def read_related_tasks(root: Path, task_id: str) -> RelatedTasks:
	"""Find the tasks a task depends on and the tasks that depend on it.

	Args:
		root: The repository worktree root.
		task_id: The reviewed chunk's task.

	Returns:
		The titles of both groups. A prerequisite that no longer exists is
		listed as a removed task with its identifier.

	Raises:
		ProgressError: Progress cannot export the project, or the export does
			not contain the task.
	"""
	# The project export, the one progress command with dependency identifiers.
	data = _read(root, "export")

	try:
		return _parse_related(data, task_id)
	except (KeyError, TypeError) as error:
		raise ProgressError(
			"The progress export has incomplete task details."
		) from error


def _parse_related(data: dict, task_id: str) -> RelatedTasks:
	"""Build the related task titles from a progress export.

	Args:
		data: The data object from progress export.
		task_id: The reviewed chunk's task identifier.

	Returns:
		The titles of prerequisite and dependent tasks.

	Raises:
		KeyError: A required field is missing.
		TypeError: A field has the wrong shape.
		ProgressError: The reviewed task is absent from the export.
	"""
	# Every task in the project.
	tasks = data["tasks"]
	# The reviewed chunk's task, or None when the export lacks it.
	current = next((task for task in tasks if task["id"] == task_id), None)

	if current is None:
		raise ProgressError(f"The progress export does not contain task {task_id}.")

	# Task titles looked up by identifier.
	titles = {task["id"]: task["title"] for task in tasks}

	# Titles of the tasks this task needs first.
	prerequisites = [
		titles.get(item, f"Removed task ({item})")
		for item in current.get("dependency_ids", [])
	]

	# Titles of the tasks waiting for this task.
	dependents = [
		task["title"] for task in tasks if task_id in task.get("dependency_ids", [])
	]

	return RelatedTasks(tuple(prerequisites), tuple(dependents))


def _read(root: Path, *arguments: str) -> dict:
	"""Run one progress command and return its data.

	Args:
		root: The folder to run progress in.
		*arguments: The progress subcommand and its arguments, without --json.

	Returns:
		The data object from the JSON envelope.

	Raises:
		ProgressError: Progress is missing, times out, prints invalid JSON or
			reports a failure.
	"""
	# The command as the user would type it, for error messages.
	command = " ".join(arguments)

	try:
		# The finished progress process.
		result = subprocess.run(
			["progress", *arguments, "--json"],
			cwd=root,
			capture_output=True,
			text=True,
			timeout=PROGRESS_TIMEOUT,
			check=False,
		)
		# The parsed JSON envelope.
		packet = json.loads(result.stdout)
	except (OSError, subprocess.TimeoutExpired, ValueError) as error:
		raise ProgressError(f"Could not read progress {command}.") from error

	if not isinstance(packet, dict):
		raise ProgressError(f"Progress returned an invalid response for {command}.")

	if result.returncode or packet.get("ok") is not True:
		# Progress's error details, or none when they are not an object.
		error_data = packet["error"] if isinstance(packet.get("error"), dict) else {}
		# Progress's own explanation, or a general one when it gives none.
		message = error_data.get("message", f"Progress could not read {command}.")

		raise ProgressError(message, error_data.get("code", "environment"))

	try:
		return packet["data"]
	except KeyError as error:
		raise ProgressError(f"Progress returned no data for {command}.") from error


def _parse_chunk(data: dict) -> Chunk:
	"""Build a chunk from its progress record.

	Args:
		data: The data object from progress chunk get.

	Returns:
		The chunk and its task. Missing optional text becomes an empty string.

	Raises:
		KeyError: A required field is missing.
		TypeError: A field has the wrong shape.
	"""
	# The parent task record inside the chunk record.
	task_data = data["task"]

	# Every chunk in the parent task.
	chunks = tuple(
		TaskChunk(item["id"], item["title"], item["status"])
		for item in task_data.get("chunks", [])
	)

	# The parent task.
	task = Task(
		task_data["id"],
		task_data["title"],
		task_data.get("overview") or "",
		tuple(task_data.get("done_when", [])),
		chunks,
	)

	return Chunk(
		data["id"],
		data["title"],
		data.get("description") or "",
		data.get("review_question") or "",
		task,
	)
