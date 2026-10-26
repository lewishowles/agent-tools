"""Check task selection, background refreshes and failed progress lookups."""

import json
import subprocess
from pathlib import Path

import pytest
from agent_board import progress_lookup


def _response(document: dict, returncode: int = 0) -> subprocess.CompletedProcess:
    """Build one progress command result for a worker test.

    Args:
        document: The JSON document returned by progress.
        returncode: The command's process status.

    Returns:
        A completed process with the selected JSON output.
    """
    return subprocess.CompletedProcess([], returncode, json.dumps(document), "")


def _task_response(chunk: str | None = "Write tests") -> subprocess.CompletedProcess:
    """Return a usable task with an optional active chunk.

    Args:
        chunk: The active chunk title, or None when there is no active chunk.

    Returns:
        A successful progress command result.
    """
    return _response(
        {
            "ok": True,
            "data": {
                "task": {"title": "Show team tasks"},
                "chunk": None if chunk is None else {"title": chunk},
            },
        }
    )


@pytest.fixture
def inline_workers(monkeypatch) -> None:
    """Finish each daemon worker inside the test without creating a thread."""

    class Worker:
        """Hold the background job until its start call."""

        def __init__(self, target, args, daemon):
            """Require daemon work and retain its call arguments."""
            assert daemon is True
            self.target = target
            self.args = args

        def start(self):
            """Run the progress lookup before returning to the test."""
            self.target(*self.args)

    monkeypatch.setattr(progress_lookup.threading, "Thread", Worker)


def test_task_worktree_lookup_uses_whole_id_and_member_subfolder(
    monkeypatch, inline_workers
) -> None:
    """A member inside a task worktree selects its team's task ID."""
    calls = []
    monkeypatch.setattr(Path, "home", lambda: Path("/home/lewis"))

    def listed_task(command, **kwargs):
        """Record the command and return the task's active chunk."""
        calls.append((command, kwargs))
        return _task_response()

    monkeypatch.setattr(progress_lookup.subprocess, "run", listed_task)
    reader = progress_lookup.ProgressReader()
    directory = "/home/lewis/.agents/worktrees/prj_abc-123/tsk_id-part_2/src"

    reader.update({"team": directory})

    assert reader.snapshot() == {
        "team": progress_lookup.TaskReading("Show team tasks", "Write tests")
    }
    assert calls[0][0] == ["progress", "next", "--task", "tsk_id-part_2", "--json"]
    assert calls[0][1]["cwd"] == directory
    assert calls[0][1]["timeout"] == progress_lookup.LOOKUP_TIMEOUT_SECONDS


def test_plain_checkout_uses_next_task_and_no_chunk(
    monkeypatch, inline_workers
) -> None:
    """A directory outside a task worktree asks for the project's next task."""
    commands = []

    def listed_task(command, **kwargs):
        """Record the plain-checkout command and return its next task."""
        commands.append(command)
        return _task_response(None)

    monkeypatch.setattr(progress_lookup.subprocess, "run", listed_task)
    reader = progress_lookup.ProgressReader()

    reader.update({"team": "/work/project"})

    assert commands == [["progress", "next", "--json"]]
    assert reader.snapshot()["team"] == progress_lookup.TaskReading(
        "Show team tasks", None
    )


def test_refreshes_only_when_due_and_drops_absent_teams(monkeypatch) -> None:
    """The first lookup is background work; later listings respect 60 seconds."""
    workers = []
    clock = [100]
    calls = []

    class Worker:
        """Queue the check until the test explicitly completes it."""

        def __init__(self, target, args, daemon):
            """Require a daemon thread and keep its work for later."""
            assert daemon is True
            self.run = lambda: target(*args)

        def start(self):
            """Queue this worker without blocking the board."""
            workers.append(self.run)

    def listed_task(command, **kwargs):
        """Count external checks and return one task."""
        calls.append(kwargs["cwd"])
        return _task_response()

    monkeypatch.setattr(progress_lookup.threading, "Thread", Worker)
    monkeypatch.setattr(progress_lookup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(progress_lookup.subprocess, "run", listed_task)
    reader = progress_lookup.ProgressReader()

    reader.update({"team": "/first"})

    assert calls == []
    assert len(workers) == 1

    reader.update({"team": "/first"})

    assert len(workers) == 1

    workers.pop()()

    clock[0] += 59
    reader.update({"team": "/first"})

    assert len(workers) == 0

    clock[0] += 1
    reader.update({"team": "/first"})

    assert len(workers) == 1

    workers.pop()()

    assert calls == ["/first", "/first"]

    reader.update({})

    assert reader.snapshot() == {}


def test_changed_directory_checks_immediately_and_ignores_old_worker(
    monkeypatch,
) -> None:
    """An old worker cannot restore the previous directory's task."""
    workers = []

    class Worker:
        """Defer each check until the test chooses an order."""

        def __init__(self, target, args, daemon):
            """Keep the worker and require a daemon thread."""
            assert daemon is True
            self.run = lambda: target(*args)

        def start(self):
            """Queue the lookup without running it."""
            workers.append(self.run)

    def listed_task(command, **kwargs):
        """Give the old and new directories different titles."""
        return _response(
            {
                "ok": True,
                "data": {
                    "task": {"title": kwargs["cwd"]},
                    "chunk": None,
                },
            }
        )

    monkeypatch.setattr(progress_lookup.threading, "Thread", Worker)
    monkeypatch.setattr(progress_lookup.subprocess, "run", listed_task)
    reader = progress_lookup.ProgressReader()

    reader.update({"team": "/old"})
    reader.update({"team": "/new"})

    assert reader.snapshot() == {}

    workers.pop(1)()
    workers.pop(0)()

    assert reader.snapshot()["team"].task == "/new"


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(FileNotFoundError("progress"), id="missing-progress"),
        pytest.param(
            _response({"ok": False, "error": {"code": "not-a-project"}}, 1),
            id="not-a-project",
        ),
        pytest.param(
            _response({"ok": False, "error": {"code": "uninitialised-project"}}, 1),
            id="uninitialised-project",
        ),
        pytest.param(
            _response({"ok": False, "error": {"code": "not-found"}}, 1),
            id="unknown-task",
        ),
        pytest.param(
            _response({"ok": True, "data": {"task": None, "chunk": None}}), id="no-task"
        ),
    ],
)
def test_definitive_absence_clears_saved_task(
    monkeypatch, inline_workers, failure
) -> None:
    """A missing command, project or task removes the previous reading."""
    responses = iter([_task_response(), failure])
    clock = [100]

    def listed_task(command, **kwargs):
        """Return the next result or raise the selected command failure."""
        response = next(responses)
        if isinstance(response, BaseException):
            raise response

        return response

    monkeypatch.setattr(progress_lookup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(progress_lookup.subprocess, "run", listed_task)
    reader = progress_lookup.ProgressReader()
    reader.update({"team": "/project"})

    assert reader.snapshot() == {
        "team": progress_lookup.TaskReading("Show team tasks", "Write tests")
    }

    clock[0] += 60

    reader.update({"team": "/project"})

    assert reader.snapshot() == {}


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            subprocess.TimeoutExpired(
                ["progress"], progress_lookup.LOOKUP_TIMEOUT_SECONDS
            ),
            id="timeout",
        ),
        pytest.param(PermissionError("temporary failure"), id="other-os-error"),
        pytest.param(subprocess.CompletedProcess([], 0, "{bad", ""), id="bad-json"),
        pytest.param(
            _response({"ok": False, "error": {"code": "busy"}}, 1),
            id="other-error-code",
        ),
    ],
)
def test_temporary_failure_keeps_saved_task(
    monkeypatch, inline_workers, failure
) -> None:
    """A failed refresh leaves the last good task visible without error text."""
    responses = iter([_task_response(), failure])
    clock = [100]

    def listed_task(command, **kwargs):
        """Return the saved task, then the selected transient failure."""
        response = next(responses)
        if isinstance(response, BaseException):
            raise response

        return response

    monkeypatch.setattr(progress_lookup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(progress_lookup.subprocess, "run", listed_task)
    reader = progress_lookup.ProgressReader()
    reader.update({"team": "/project"})

    assert reader.snapshot() == {
        "team": progress_lookup.TaskReading("Show team tasks", "Write tests")
    }

    clock[0] += 60

    reader.update({"team": "/project"})

    assert reader.snapshot() == {
        "team": progress_lookup.TaskReading("Show team tasks", "Write tests")
    }
