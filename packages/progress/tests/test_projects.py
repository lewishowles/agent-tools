import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from agents_progress import projects as projects_module, worktrees as worktrees_module
from agents_progress.database import Database
from agents_progress.errors import (
	AlreadyExistsError,
	NotAProjectError,
	NotFoundError,
	OrphanedProjectError,
	ProgressError,
	ProjectBindingRecoveryError,
	UninitialisedProjectError,
	UnresolvedDependenciesError,
	WrongObjectIdTypeError,
)
from agents_progress.ids import PROJECT_PREFIX, generate_object_id
from agents_progress.projects import ProjectStore
from agents_progress.repository import GitRepository
from agents_progress.worktrees import WorktreeStore
from agents_progress.writes import WriteStore


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


def test_detach_checkouts_resolves_paths_and_removes_only_selected_records(
	tmp_path, monkeypatch
) -> None:
	repository = _git_repository(tmp_path / "repository")
	other = _git_repository(tmp_path / "other")
	store = ProjectStore(Database(tmp_path / "progress.db"))
	project, _ = store.init("agents", "Agents", repository)
	store.attach(project.id, other)
	store.record_checkout(repository)
	store.record_checkout(other)
	monkeypatch.chdir(tmp_path)

	removed = store.detach_checkouts(["repository", repository])

	assert removed == [str(repository)]
	with store.database.connection() as connection:
		paths = [
			row["path"] for row in connection.execute("SELECT path FROM checkouts")
		]
	assert paths == [str(other)]


def test_detach_checkouts_removes_stale_paths_and_keeps_live_paths(tmp_path) -> None:
	repository = _git_repository(tmp_path / "repository")
	missing = tmp_path / "missing"
	store = ProjectStore(Database(tmp_path / "progress.db"))
	project, _ = store.init("agents", "Agents", repository)
	store.record_checkout(repository)
	with store.database.transaction() as connection:
		connection.execute(
			"INSERT INTO checkouts (path, project_id, last_seen_at) VALUES (?, ?, ?)",
			(str(missing), project.id, "2026-01-01T00:00:00+00:00"),
		)

	assert store.detach_checkouts(stale=True) == [str(missing)]
	with store.database.connection() as connection:
		paths = [
			row["path"] for row in connection.execute("SELECT path FROM checkouts")
		]
	assert paths == [str(repository)]


def test_detach_checkouts_detaches_nothing_when_a_named_path_was_never_recorded(
	tmp_path,
) -> None:
	repository = _git_repository(tmp_path / "repository")
	store = ProjectStore(Database(tmp_path / "progress.db"))
	store.init("agents", "Agents", repository)
	store.record_checkout(repository)
	missing = tmp_path / "missing"

	with pytest.raises(NotFoundError) as error:
		store.detach_checkouts([repository, missing], stale=True)

	assert error.value.details == {"path": str(missing)}
	with store.database.connection() as connection:
		paths = [
			row["path"] for row in connection.execute("SELECT path FROM checkouts")
		]
	assert paths == [str(repository)]


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


@pytest.fixture
def managed_repository(tmp_path, committed_repository):
	"""Provide a bound repository with a committed HEAD and one ready task."""
	repository = committed_repository
	database = Database(tmp_path / "progress.db")
	project, _ = ProjectStore(database).init(
		"agents", "Agent configuration", repository
	)
	task = WriteStore(database).task_add(
		"first-task",
		"First task",
		"Work in isolation",
		["Finish the work"],
		path=repository,
	)
	return repository, database, project.id, task["id"]


def test_worktree_ensure_creates_from_head_and_reuses_edits(managed_repository) -> None:
	repository, database, project_id, task_id = managed_repository
	store = WorktreeStore(database)
	head = subprocess.run(
		["git", "-C", str(repository), "rev-parse", "HEAD"],
		capture_output=True,
		check=True,
		text=True,
	).stdout.strip()
	(repository / "tracked.txt").write_text("uncommitted content\n")
	(repository / "source-only.txt").write_text("untracked content\n")

	created = store.ensure(task_id, repository)
	checkout = Path(created["path"])
	checkout_head = subprocess.run(
		["git", "-C", str(checkout), "rev-parse", "HEAD"],
		capture_output=True,
		check=True,
		text=True,
	).stdout.strip()

	assert created == {
		"task_id": task_id,
		"project_id": project_id,
		"path": str(database.path.parent / "worktrees" / project_id / task_id),
		"branch": f"progress/task/{task_id}",
		"created": True,
	}
	assert checkout_head == head
	assert (checkout / "tracked.txt").read_text() == "committed content\n"
	assert not (checkout / "source-only.txt").exists()
	assert (
		GitRepository(checkout).common_dir() == GitRepository(repository).common_dir()
	)
	assert ProjectStore(database).current(checkout).id == project_id
	assert GitRepository(repository).get_binding() == project_id

	(checkout / "tracked.txt").write_text("task edit\n")
	reused = store.ensure(task_id, checkout)

	assert reused == {**created, "created": False}
	assert store.get(task_id, repository) == {
		key: value for key, value in created.items() if key != "created"
	}
	assert (checkout / "tracked.txt").read_text() == "task edit\n"


def test_worktree_ensure_removes_checkout_and_branch_when_recording_fails(
	managed_repository, monkeypatch
) -> None:
	repository, database, project_id, task_id = managed_repository
	target = database.path.parent / "worktrees" / project_id / task_id
	branch = f"progress/task/{task_id}"

	def fail_timestamp() -> str:
		raise RuntimeError("recording failed")

	monkeypatch.setattr(worktrees_module, "utc_timestamp", fail_timestamp)

	with pytest.raises(RuntimeError, match="recording failed"):
		WorktreeStore(database).ensure(task_id, repository)

	branch_result = GitRepository(repository).run(
		["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
	)

	assert not target.exists()
	assert branch_result.returncode == 1
	with database.connection() as connection:
		assert (
			connection.execute("SELECT COUNT(*) FROM task_worktrees").fetchone()[0] == 0
		)


def test_worktree_ensure_reports_branch_cleanup_failure(
	managed_repository, monkeypatch
) -> None:
	repository, database, project_id, task_id = managed_repository
	target = database.path.parent / "worktrees" / project_id / task_id
	branch = f"progress/task/{task_id}"
	cleanup_error = "branch cleanup failed"
	cleanup_message = f"git branch -d {branch} failed: {cleanup_error}"
	original_run = GitRepository.run

	def fail_timestamp() -> str:
		raise ProgressError("recording failed", {"task_id": task_id})

	def fail_branch_cleanup(self, arguments):
		if arguments == ["branch", "-d", branch]:
			return subprocess.CompletedProcess(arguments, 1, "", f"{cleanup_error}\n")

		return original_run(self, arguments)

	monkeypatch.setattr(worktrees_module, "utc_timestamp", fail_timestamp)
	monkeypatch.setattr(GitRepository, "run", fail_branch_cleanup)

	with pytest.raises(ProgressError) as error:
		WorktreeStore(database).ensure(task_id, repository)

	assert error.value.message.endswith(f"; {cleanup_message}")
	assert error.value.details == {
		"task_id": task_id,
		"path": str(target),
		"branch": branch,
		"cleanup_error": cleanup_error,
	}
	assert error.value.__notes__ == [cleanup_message]


def test_worktree_ensure_reports_worktree_cleanup_failure(
	managed_repository, monkeypatch
) -> None:
	repository, database, project_id, task_id = managed_repository
	target = database.path.parent / "worktrees" / project_id / task_id
	cleanup_error = "worktree cleanup failed"
	cleanup_message = f"git worktree remove {target} failed: {cleanup_error}"
	original_run = GitRepository.run
	commands = []

	def fail_timestamp() -> str:
		raise ProgressError("recording failed")

	def fail_worktree_cleanup(self, arguments):
		commands.append(arguments)

		if arguments == ["worktree", "remove", str(target)]:
			return subprocess.CompletedProcess(arguments, 1, "", f"{cleanup_error}\n")

		return original_run(self, arguments)

	monkeypatch.setattr(worktrees_module, "utc_timestamp", fail_timestamp)
	monkeypatch.setattr(GitRepository, "run", fail_worktree_cleanup)

	with pytest.raises(ProgressError) as error:
		WorktreeStore(database).ensure(task_id, repository)

	assert error.value.message.endswith(f"; {cleanup_message}")
	assert error.value.details == {
		"path": str(target),
		"cleanup_error": cleanup_error,
	}
	assert error.value.__notes__ == [cleanup_message]
	assert not any(command[:2] == ["branch", "-d"] for command in commands)


def test_worktree_ensure_refuses_other_repositories_and_collisions(
	managed_repository, tmp_path
) -> None:
	repository, database, project_id, task_id = managed_repository
	store = WorktreeStore(database)
	store.ensure(task_id, repository)
	other_repository = _git_repository(tmp_path / "other-repository")
	ProjectStore(database).attach(project_id, other_repository)

	with pytest.raises(AlreadyExistsError, match="another checkout or project"):
		store.get(task_id, other_repository)
	with pytest.raises(AlreadyExistsError, match="another checkout or project"):
		store.ensure(task_id, other_repository)

	branch_task = WriteStore(database).task_add(
		"branch-task",
		"Branch task",
		"Check branch collisions",
		["Finish"],
		path=repository,
	)
	subprocess.run(
		["git", "-C", str(repository), "branch", f"progress/task/{branch_task['id']}"],
		check=True,
	)
	with pytest.raises(AlreadyExistsError, match="branch"):
		store.ensure(branch_task["id"], repository)

	path_task = WriteStore(database).task_add(
		"path-task", "Path task", "Check path collisions", ["Finish"], path=repository
	)
	target = database.path.parent / "worktrees" / project_id / path_task["id"]
	target.mkdir(parents=True)
	with pytest.raises(AlreadyExistsError, match="path"):
		store.ensure(path_task["id"], repository)
	with pytest.raises(NotFoundError, match="no managed worktree"):
		store.get(path_task["id"], repository)


def test_worktree_ensure_refuses_unfinished_dependencies(managed_repository) -> None:
	repository, database, _, task_id = managed_repository
	dependent = WriteStore(database).task_add(
		"dependent",
		"Dependent task",
		"Wait for the first task",
		["Finish"],
		depends_on=[task_id],
		path=repository,
	)

	with pytest.raises(UnresolvedDependenciesError) as error:
		WorktreeStore(database).ensure(dependent["id"], repository)

	assert error.value.details["dependencies"] == [task_id]


def test_worktree_get_refuses_a_checkout_moved_to_another_branch(
	managed_repository,
) -> None:
	repository, database, _, task_id = managed_repository
	store = WorktreeStore(database)
	checkout = Path(store.ensure(task_id, repository)["path"])
	subprocess.run(
		["git", "-C", str(checkout), "switch", "-c", "different-branch"], check=True
	)

	with pytest.raises(AlreadyExistsError, match="not on branch"):
		store.get(task_id, repository)
	with pytest.raises(AlreadyExistsError, match="not on branch"):
		store.ensure(task_id, repository)


def test_worktree_ensure_refuses_a_path_recorded_for_another_task(
	managed_repository,
) -> None:
	repository, database, project_id, task_id = managed_repository
	store = WorktreeStore(database)
	store.ensure(task_id, repository)
	other_task = WriteStore(database).task_add(
		"other-task", "Other task", "Check task ownership", ["Finish"], path=repository
	)
	target = database.path.parent / "worktrees" / project_id / other_task["id"]
	with database.transaction() as connection:
		connection.execute(
			"UPDATE task_worktrees SET path = ? WHERE task_id = ?",
			(str(target), task_id),
		)

	with pytest.raises(AlreadyExistsError, match="path"):
		store.ensure(other_task["id"], repository)
