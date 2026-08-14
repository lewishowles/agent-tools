"""Project records and their repository-local Git bindings."""

from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .database import Database
from .errors import (
	AlreadyInitialisedError,
	EmptyValueError,
	NotFoundError,
	OrphanedProjectError,
	ProjectBindingRecoveryError,
	UninitialisedProjectError,
)
from .ids import PROJECT_PREFIX, generate_object_id, validate_object_id
from .repository import GitRepository
from .schema import utc_timestamp


@dataclass(frozen=True)
class Project:
	"""Represent the public fields of a stored project."""

	id: str
	slug: str
	name: str
	created_at: str

	@classmethod
	def from_row(cls, row: object) -> "Project":
		"""Build a Project from a sqlite3.Row returned by a projects query."""
		return cls(row["id"], row["slug"], row["name"], row["created_at"])

	def to_dict(self) -> dict[str, str]:
		"""Return the project's public fields, leaving out the internal created_at timestamp."""
		return {"id": self.id, "slug": self.slug, "name": self.name}


class BindingRepository(Protocol):
	"""Provide the Git binding operations required by ProjectStore."""

	def root(self) -> Path: ...

	def get_binding(self) -> str | None: ...

	def set_binding(self, project_id: str) -> None: ...

	def clear_binding(self) -> None: ...


class ProjectStore:
	"""Coordinate project rows with repository-local Git bindings."""

	def __init__(
		self,
		database: Database | None = None,
		repository_factory: type[GitRepository] = GitRepository,
	) -> None:
		self.database = database or Database()
		self.repository_factory = repository_factory

	@contextmanager
	def _repository(self, path: str | Path | None) -> Iterator[BindingRepository]:
		"""Open a Git repository at path, raising if it isn't one."""
		repository = self.repository_factory(path)
		repository.root()
		yield repository

	def _project_exists(self, project_id: str) -> bool:
		"""Return whether a project row with this ID already exists."""
		with self.database.connection() as connection:
			return (
				connection.execute(
					"SELECT 1 FROM projects WHERE id = ?",
					(project_id,),
				).fetchone()
				is not None
			)

	def _find(self, project_id: str) -> Project | None:
		"""Return the stored project for an ID, or None when it doesn't exist."""
		with self.database.connection() as connection:
			row = connection.execute(
				"SELECT id, slug, name, created_at FROM projects WHERE id = ?",
				(project_id,),
			).fetchone()

		return Project.from_row(row) if row is not None else None

	def _compensate(
		self,
		repository: BindingRepository,
		previous_binding: str | None,
		project_id: str,
		cause: Exception,
	) -> None:
		"""Restore the previous Git binding after a failed write, or report recovery."""
		try:
			if previous_binding is None:
				repository.clear_binding()
			else:
				repository.set_binding(previous_binding)
		except Exception as compensation_error:
			recovery_command = (
				"git config --local --unset-all progress.project-id"
				if previous_binding is None
				else f"git config --local progress.project-id {previous_binding}"
			)
			raise ProjectBindingRecoveryError(
				project_id,
				recovery_command,
				compensation_error,
			) from cause

	def init(self, slug: str, name: str, path: str | Path | None = None) -> Project:
		"""Create a project row and bind the current Git repository to it."""
		if not slug.strip() or not name.strip():
			raise EmptyValueError("project slug and name must not be empty")

		with self._repository(path) as repository:
			if repository.get_binding() is not None:
				raise AlreadyInitialisedError(
					"project init refuses to replace an existing progress.project-id binding"
				)

			project_id = generate_object_id(PROJECT_PREFIX, self._project_exists)
			project = Project(project_id, slug, name, utc_timestamp())

			try:
				repository.set_binding(project.id)
				with self.database.transaction() as connection:
					connection.execute(
						"""
						INSERT INTO projects (id, slug, name, created_at)
						VALUES (?, ?, ?, ?)
						""",
						(project.id, project.slug, project.name, project.created_at),
					)
			except Exception as error:
				self._compensate(repository, None, project.id, error)
				raise

		return project

	def attach(self, project_id: str, path: str | Path | None = None) -> Project:
		"""Link an existing project to the current Git repository."""
		validate_object_id(project_id, PROJECT_PREFIX)
		project = self._find(project_id)
		if project is None:
			raise NotFoundError(
				f"project {project_id} was not found",
				{"id": project_id},
			)

		with self._repository(path) as repository:
			previous_binding = repository.get_binding()
			if previous_binding == project_id:
				return project

			try:
				repository.set_binding(project_id)
			except Exception as error:
				self._compensate(repository, previous_binding, project_id, error)
				raise

		return project

	def current(self, path: str | Path | None = None) -> Project:
		"""Resolve the project bound to the current Git repository."""
		with self._repository(path) as repository:
			binding = repository.get_binding()
			if binding is None:
				raise UninitialisedProjectError(
					"this Git repository has no progress project; run progress project init"
				)

			try:
				validate_object_id(binding, PROJECT_PREFIX)
			except Exception as error:
				raise OrphanedProjectError(
					"progress.project-id is malformed; initialise or attach the repository explicitly",
					{"binding": binding},
				) from error

			project = self._find(binding)

		if project is None:
			raise OrphanedProjectError(
				f"progress.project-id {binding} has no database row; restore the database or run progress project init",
				{"binding": binding},
			)

		return project


class _StoreBase:
	"""Share database setup and current-project resolution across store types."""

	def __init__(
		self,
		database: Database | None = None,
		project_store: ProjectStore | None = None,
	) -> None:
		"""Let read and write stores share one database and project store."""
		if project_store is None:
			self.database = database or Database()
			self.projects = ProjectStore(self.database)
		else:
			self.projects = project_store
			self.database = database or project_store.database

	def current_project(self, path: str | Path | None = None) -> Project:
		"""Resolve the project bound to the given path (or the working directory)."""
		return self.projects.current(path)
