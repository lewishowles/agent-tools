"""Immutable records returned by the progress read surface."""

import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True)
class Release:
	"""A stored release."""

	id: str
	project_id: str
	slug: str
	title: str
	overview: str
	status: str
	position: int

	@classmethod
	def from_row(cls, row: object) -> "Release":
		return cls(**dict(row))

	def to_dict(self) -> dict[str, object]:
		return dataclasses.asdict(self)


@dataclass(frozen=True)
class Task:
	"""A stored task."""

	id: str
	project_id: str
	slug: str
	release_id: str | None
	title: str
	overview: str
	purpose: str
	contract: str
	files: str | None
	acceptance_criteria: str
	verification: str
	risks: str
	status: str
	status_reason: str | None
	position: int
	created_at: str
	started_at: str | None
	completed_at: str | None
	updated_at: str

	@classmethod
	def from_row(cls, row: object) -> "Task":
		return cls(**dict(row))

	def to_dict(self) -> dict[str, object]:
		return dataclasses.asdict(self)


@dataclass(frozen=True)
class Chunk:
	"""A stored chunk within a task."""

	id: str
	task_id: str
	position: int
	title: str
	description: str
	status: str
	started_at: str | None
	completed_at: str | None

	@classmethod
	def from_row(cls, row: object) -> "Chunk":
		return cls(**dict(row))

	def to_dict(self) -> dict[str, object]:
		return dataclasses.asdict(self)


@dataclass(frozen=True)
class Note:
	"""A stored discovery or decision note."""

	id: str
	project_id: str
	task_id: str | None
	type: str
	body: str
	supersedes_id: str | None
	created_at: str

	@classmethod
	def from_row(cls, row: object) -> "Note":
		return cls(**dict(row))

	def to_dict(self) -> dict[str, object]:
		return dataclasses.asdict(self)


@dataclass(frozen=True)
class Context:
	"""The current handoff context for one project."""

	project_id: str
	current_goal: str | None
	previous_step: str | None
	next_step: str | None
	standing_context: str | None
	verify_with: str | None
	stop_marker: str | None
	updated_at: str

	@classmethod
	def from_row(cls, row: object) -> "Context":
		return cls(**dict(row))

	def to_dict(self) -> dict[str, object]:
		return dataclasses.asdict(self)
