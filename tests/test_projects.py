from contextlib import contextmanager
from pathlib import Path
import subprocess

import pytest

from agents_progress.database import Database
from agents_progress.errors import (
	AlreadyInitialisedError,
	NotAProjectError,
	NotFoundError,
	OrphanedProjectError,
	ProjectBindingRecoveryError,
	UninitialisedProjectError,
	WrongObjectIdTypeError,
)
from agents_progress.ids import PROJECT_PREFIX, generate_object_id
from agents_progress.projects import ProjectStore
from agents_progress.repository import GitRepository


def _git_repository(path: Path) -> Path:
	path.mkdir()
	subprocess.run(["git", "init", "--quiet", str(path)], check=True)
	return path


def test_init_binds_and_current_resolves_from_a_subdirectory_after_a_move(
	tmp_path,
) -> None:
	repository = _git_repository(tmp_path / "repository")
	(repository / "src").mkdir()
	store = ProjectStore(Database(tmp_path / "progress.db"))

	project = store.init("agents", "Agent configuration", repository / "src")

	assert GitRepository(repository).get_binding() == project.id
	assert store.current(repository / "src") == project

	moved_repository = tmp_path / "moved-repository"
	repository.rename(moved_repository)
	assert store.current(moved_repository / "src") == project


def test_init_refuses_to_replace_an_existing_binding(tmp_path) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))
	store.init("agents", "Agent configuration", repository)

	with pytest.raises(AlreadyInitialisedError):
		store.init("other", "Other project", repository)


def test_current_reports_uninitialised_and_orphaned_repositories(tmp_path) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))

	with pytest.raises(UninitialisedProjectError):
		store.current(repository)

	project = store.init("agents", "Agent configuration", repository)
	with store.database.transaction() as connection:
		connection.execute("DELETE FROM projects WHERE id = ?", (project.id,))

	with pytest.raises(OrphanedProjectError, match=project.id):
		store.current(repository)


def test_non_git_paths_and_attach_validation_are_explicit(tmp_path) -> None:
	store = ProjectStore(Database(tmp_path / "progress.db"))

	with pytest.raises(NotAProjectError):
		store.current(tmp_path)

	with pytest.raises(WrongObjectIdTypeError):
		store.attach("tsk_" + "a" * 22, tmp_path)

	with pytest.raises(NotFoundError):
		store.attach(generate_object_id(PROJECT_PREFIX), tmp_path)


class _FakeRepository:
	def __init__(self, clear_error: Exception | None = None) -> None:
		self.binding: str | None = None
		self.clear_error = clear_error

	def root(self) -> Path:
		return Path("/fake/repository")

	def get_binding(self) -> str | None:
		return self.binding

	def set_binding(self, project_id: str) -> None:
		self.binding = project_id

	def clear_binding(self) -> None:
		if self.clear_error is not None:
			raise self.clear_error
		self.binding = None


def test_init_compensates_a_database_failure_and_reports_recovery_failure(
	tmp_path, monkeypatch
) -> None:
	database = Database(tmp_path / "progress.db")
	repository = _FakeRepository()
	store = ProjectStore(database, repository_factory=lambda path: repository)

	@contextmanager
	def failing_transaction():
		raise RuntimeError("database write failed")
		yield

	monkeypatch.setattr(database, "transaction", failing_transaction)

	with pytest.raises(RuntimeError, match="database write failed"):
		store.init("agents", "Agent configuration")

	assert repository.binding is None

	repository = _FakeRepository(RuntimeError("git compensation failed"))
	store = ProjectStore(database, repository_factory=lambda path: repository)
	monkeypatch.setattr(database, "transaction", failing_transaction)

	with pytest.raises(ProjectBindingRecoveryError) as error:
		store.init("agents", "Agent configuration")

	assert "git config --local --unset-all progress.project-id" in error.value.message
	assert "binding write failed: database write failed" in error.value.message
	assert "rollback failed: git compensation failed" in error.value.message
