import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from agents_progress import projects as projects_module
from agents_progress.database import Database
from agents_progress.errors import (
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

	project, already_initialised = store.init(
		"agents", "Agent configuration", repository / "src"
	)

	assert already_initialised is False

	assert GitRepository(repository).get_binding() == project.id
	assert store.current(repository / "src") == project

	moved_repository = tmp_path / "moved-repository"
	repository.rename(moved_repository)
	assert store.current(moved_repository / "src") == project


def test_init_returns_the_existing_project_without_changing_the_binding(
	tmp_path,
) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))
	project, _ = store.init("agents", "Agent configuration", repository)

	existing_project, already_initialised = store.init(
		"other", "Other project", repository
	)

	assert already_initialised is True
	assert existing_project == project
	assert GitRepository(repository).get_binding() == project.id
	with store.database.connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


@pytest.mark.parametrize("binding", ["malformed", "prj_" + "a" * 22])
def test_init_reports_orphaned_bindings_without_replacing_them(
	tmp_path, binding
) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))
	GitRepository(repository).set_binding(binding)

	with pytest.raises(OrphanedProjectError) as current_error:
		store.current(repository)
	with pytest.raises(OrphanedProjectError) as init_error:
		store.init("agents", "Agent configuration", repository)

	assert init_error.value.message == current_error.value.message
	assert init_error.value.details == current_error.value.details
	assert GitRepository(repository).get_binding() == binding


def test_current_reports_uninitialised_and_orphaned_repositories(tmp_path) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))

	with pytest.raises(UninitialisedProjectError):
		store.current(repository)

	project, _ = store.init("agents", "Agent configuration", repository)
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


def test_record_checkout_tracks_each_root_and_moves_a_rebound_path(
	tmp_path, monkeypatch
) -> None:
	first_repository = _git_repository(tmp_path / "first")
	second_repository = _git_repository(tmp_path / "second")
	third_repository = _git_repository(tmp_path / "third")
	(second_repository / "src").mkdir()
	store = ProjectStore(Database(tmp_path / "progress.db"))
	first_project, _ = store.init("first", "First project", first_repository)
	second_project, _ = store.init("second", "Second project", third_repository)
	store.attach(first_project.id, second_repository)
	timestamps = iter(
		[
			"2026-01-01T00:00:00+00:00",
			"2026-01-02T00:00:00+00:00",
			"2026-01-03T00:00:00+00:00",
			"2026-01-04T00:00:00+00:00",
		]
	)
	monkeypatch.setattr(projects_module, "utc_timestamp", lambda: next(timestamps))

	store.record_checkout(first_repository)
	store.record_checkout(second_repository / "src")
	store.record_checkout(second_repository)
	store.attach(second_project.id, second_repository)
	store.record_checkout(second_repository)

	with store.database.connection() as connection:
		rows = connection.execute(
			"SELECT path, project_id, last_seen_at FROM checkouts ORDER BY path"
		).fetchall()

	assert [tuple(row) for row in rows] == [
		(str(first_repository), first_project.id, "2026-01-01T00:00:00+00:00"),
		(str(second_repository), second_project.id, "2026-01-04T00:00:00+00:00"),
	]


def test_record_checkout_tracks_a_linked_worktree_for_the_same_project(
	tmp_path,
) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))
	project, _ = store.init("agents", "Agent configuration", repository)
	subprocess.run(
		[
			"git",
			"-C",
			str(repository),
			"-c",
			"user.name=Test",
			"-c",
			"user.email=test@example.com",
			"commit",
			"--quiet",
			"--allow-empty",
			"-m",
			"Initial commit",
		],
		check=True,
	)
	worktree = tmp_path / "linked-worktree"
	subprocess.run(
		["git", "-C", str(repository), "worktree", "add", "--detach", str(worktree)],
		capture_output=True,
		check=True,
	)
	(worktree / "src").mkdir()

	store.record_checkout(repository)
	store.record_checkout(worktree / "src")

	with store.database.connection() as connection:
		rows = connection.execute("SELECT path, project_id FROM checkouts").fetchall()

	assert {tuple(row) for row in rows} == {
		(str(repository), project.id),
		(str(worktree), project.id),
	}


def test_record_checkout_skips_an_unbound_repository(tmp_path) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))

	store.record_checkout(repository)

	with store.database.connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 0


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
