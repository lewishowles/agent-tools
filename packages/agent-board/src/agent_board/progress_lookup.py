"""Find each listed team's progress task without holding up the board."""

import json
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

# The shortest gap between checks for a team whose directory has not changed.
CHECK_INTERVAL_SECONDS = 60
# How long one progress command may run before the check counts as failed and
# the team keeps its last task.
LOOKUP_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class TaskReading:
    """The task and current chunk that the board can show for a team."""

    # The task title shown beneath the team.
    task: str
    # The current chunk title, or None when the task has no active chunk.
    chunk: str | None


@dataclass
class _TeamEntry:
    """One team's directory, its latest task and when it was last checked."""

    # The directory used to ask progress about this team.
    directory: str
    # The latest usable task, if there is one.
    reading: TaskReading | None = None
    # The start of the latest check, using the monotonic clock.
    last_started: float = float("-inf")
    # Whether this entry already has a check in flight.
    checking: bool = False


class ProgressReader:
    """Cache each live team's task and refresh it on daemon threads."""

    def __init__(self) -> None:
        """Start without any teams or task readings."""
        # Protect entries shared with the worker threads.
        self._lock = threading.Lock()
        # One entry per team key, replaced when its directory changes.
        self._entries: dict[str, _TeamEntry] = {}

    def update(self, directories: Mapping[str, str]) -> None:
        """Follow the current teams and start checks that are due.

        Args:
            directories: Each live team's key and selected working directory.
        """
        # The current time decides which existing teams need another check.
        now = time.monotonic()
        # Start these checks once the lock is released.
        due: list[tuple[str, _TeamEntry]] = []

        with self._lock:
            for key in list(self._entries):
                if key not in directories:
                    del self._entries[key]

            for key, directory in directories.items():
                entry = self._entries.get(key)

                # A changed directory gets a new entry, so a check still
                # running for the old directory cannot save its result here.
                if entry is None or entry.directory != directory:
                    entry = _TeamEntry(directory)
                    self._entries[key] = entry

                if not directory or entry.checking:
                    continue

                if now - entry.last_started >= CHECK_INTERVAL_SECONDS:
                    entry.last_started = now
                    entry.checking = True
                    due.append((key, entry))

        for key, entry in due:
            threading.Thread(target=self._check, args=(key, entry), daemon=True).start()

    def snapshot(self) -> Mapping[str, TaskReading]:
        """Return a read-only copy of the current teams' tasks.

        Returns:
            Task readings keyed by team. Teams without a known task are left
            out.
        """
        with self._lock:
            readings = {
                key: entry.reading
                for key, entry in self._entries.items()
                if entry.reading is not None
            }

        return MappingProxyType(readings)

    def _check(self, key: str, entry: _TeamEntry) -> None:
        """Look up a team's task and save it if the team still uses this entry.

        A missing command or directory clears the saved task. Any other
        failure keeps it, so a slow or broken check does not blank the board.

        Args:
            key: The board key for the team being checked.
            entry: The team's entry when this check began.
        """
        try:
            reading = _lookup(entry.directory)
        except FileNotFoundError:
            reading = None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            reading = entry.reading

        with self._lock:
            if self._entries.get(key) is entry:
                entry.reading = reading
                entry.checking = False


def _lookup(directory: str) -> TaskReading | None:
    """Ask progress for a team's own task, or the next task in a checkout.

    Args:
        directory: The working directory selected from the team's members.

    Returns:
        The task and current chunk, or None when progress has no task here.

    Raises:
        FileNotFoundError: progress is not installed or the directory is gone.
        OSError: The command cannot be started for another reason.
        subprocess.TimeoutExpired: The command takes too long.
        ValueError: The command gives an unusable or temporary response.
    """
    # A task worktree has its own task rather than the project's next task.
    task_id = _task_id(directory)
    # Ask progress for the task that belongs to this directory.
    command = ["progress", "next"]

    if task_id is not None:
        command.extend(["--task", task_id])

    command.append("--json")
    # A failed run is still read, because progress prints its errors as JSON.
    result = subprocess.run(
        command,
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=LOOKUP_TIMEOUT_SECONDS,
        check=False,
    )

    try:
        # The JSON document progress prints for both results and errors.
        document = json.loads(result.stdout)

        if document["ok"] is False:
            # The code separates a team with no task from a temporary failure.
            code = document["error"]["code"]

            if code in {"not-a-project", "uninitialised-project", "not-found"}:
                return None

            raise ValueError("progress lookup failed")

        if document["ok"] is not True or result.returncode:
            raise ValueError("progress returned an invalid response")

        # The successful response holds the task and current chunk.
        data = document["data"]
        # The task is None when progress has nothing for this directory.
        task = data["task"]

        if task is None:
            return None

        # The board shows the task's title, not its ID.
        task_title = task["title"]
        # The active chunk, or None when the task has none.
        chunk = data["chunk"]
        # The chunk title, or None when no chunk is active.
        chunk_title = None if chunk is None else chunk["title"]

        if not isinstance(task_title, str) or (
            chunk_title is not None and not isinstance(chunk_title, str)
        ):
            raise ValueError("progress returned an invalid task")

        return TaskReading(task_title, chunk_title)
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("progress returned an unusable response") from error


def _task_id(directory: str) -> str | None:
    """Return the task ID when the directory is inside a task worktree.

    Args:
        directory: The member's working directory, possibly inside a task.

    Returns:
        The task worktree's directory name, or None for a plain checkout.
    """
    # Only directories under the managed worktree root can name a task.
    worktrees = Path.home() / ".agents" / "worktrees"

    try:
        # The project and task folder names, then any subfolders.
        parts = Path(directory).relative_to(worktrees).parts
    except ValueError:
        return None

    if len(parts) < 2 or not parts[0].startswith("prj_"):
        return None

    return parts[1] if parts[1].startswith("tsk_") else None
