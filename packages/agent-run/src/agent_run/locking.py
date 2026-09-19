"""Hold advisory locks for named command runs."""

import errno
import fcntl
import hashlib
import os
from pathlib import Path

from agent_run.database import resolve_database_path

# Folder beside the database that holds one lock file per repository command.
LOCK_DIRECTORY_NAME = "agent-run-locks"


class RunBusyError(RuntimeError):
    """Raised when another run of the same named command is still active."""

    def __init__(self, run_id: str) -> None:
        """Keep the active run's ID so the refusal can name it."""
        self.run_id = run_id
        super().__init__(f"Run {run_id} is already active.")


class RunLock:
    """Hold one repository command lock until the run has finished."""

    def __init__(self, path: Path, run_id: str) -> None:
        """Create a lock that will record ``run_id`` when acquired."""
        self.path = path
        self.run_id = run_id
        self._descriptor: int | None = None

    def acquire(self) -> "RunLock":
        """Take the lock, or raise RunBusyError naming the run that holds it."""
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        os.fchmod(descriptor, 0o600)

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(descriptor)
                raise

            active_run_id = _read_run_id(descriptor)
            os.close(descriptor)
            raise RunBusyError(active_run_id) from error

        try:
            _write_run_id(descriptor, self.run_id)
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            raise

        self._descriptor = descriptor

        return self

    def release(self) -> None:
        """Empty the lock file, release the lock, and close the file.

        Emptying the file first means a later run that finds the lock busy
        never names a run that has already finished.
        """
        if self._descriptor is None:
            return

        descriptor = self._descriptor
        self._descriptor = None

        try:
            os.ftruncate(descriptor, 0)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(descriptor)


def resolve_lock_directory(database_path: str | Path | None = None) -> Path:
    """Return the directory that stores named-run lock files."""
    return (
        resolve_database_path(database_path).expanduser().resolve().parent
        / LOCK_DIRECTORY_NAME
    )


def resolve_lock_path(
    database_path: str | Path | None,
    repository_id: str,
    command_name: str,
) -> Path:
    """Return the lock file shared by every run of one command in one repository.

    The key is hashed so that any command name makes a safe file name.
    """
    lock_key = hashlib.sha256(f"{repository_id}\0{command_name}".encode()).hexdigest()
    return resolve_lock_directory(database_path) / f"{lock_key}.lock"


def acquire_run_lock(
    database_path: str | Path | None,
    repository_id: str,
    command_name: str,
    *,
    run_id: str,
) -> RunLock:
    """Take the lock for a named run before it starts.

    Raises ``RunBusyError`` when another run of the command holds it. The
    operating system releases the lock when the holding process exits, so a
    crashed run never leaves the command blocked.
    """
    lock_directory = resolve_lock_directory(database_path)
    lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock_directory, 0o700)

    lock = RunLock(
        path=resolve_lock_path(database_path, repository_id, command_name),
        run_id=run_id,
    )
    return lock.acquire()


def _read_run_id(descriptor: int) -> str:
    """Read the run ID written by the current lock holder.

    Returns "unknown" when the holder has not written its ID yet.
    """
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        run_id = os.read(descriptor, 128).decode("utf-8").strip()
    except OSError:
        run_id = ""

    return run_id or "unknown"


def _write_run_id(descriptor: int, run_id: str) -> None:
    """Replace the lock contents with the active run ID."""
    encoded_run_id = run_id.encode("utf-8")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, encoded_run_id)
    os.fsync(descriptor)
