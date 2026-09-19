"""Tests for named command advisory locks."""

import os
import select
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from agent_run.locking import RunBusyError, acquire_run_lock, resolve_lock_directory


def test_lock_refuses_an_active_command_and_allows_it_after_release(
    tmp_path: Path,
) -> None:
    """The same repository command cannot hold two locks at once."""
    database_path = tmp_path / "agent-run.db"
    first_lock = acquire_run_lock(
        database_path,
        "repository-id",
        "build",
        run_id=uuid4().hex,
    )

    try:
        with pytest.raises(RunBusyError) as error:
            acquire_run_lock(
                database_path,
                "repository-id",
                "build",
                run_id=uuid4().hex,
            )

        assert error.value.run_id == first_lock.run_id
    finally:
        first_lock.release()

    second_lock = acquire_run_lock(
        database_path,
        "repository-id",
        "build",
        run_id=uuid4().hex,
    )
    second_lock.release()

    assert resolve_lock_directory(database_path).is_dir()


def test_lock_file_is_empty_after_release(tmp_path: Path) -> None:
    """Releasing a lock removes the run ID from its file."""
    database_path = tmp_path / "agent-run.db"
    lock = acquire_run_lock(
        database_path,
        "repository-id",
        "build",
        run_id=uuid4().hex,
    )
    lock_path = lock.path

    lock.release()

    assert lock_path.read_bytes() == b""


def test_lock_is_released_when_the_holder_process_is_killed(tmp_path: Path) -> None:
    """A killed holder does not leave a stale lock that blocks the next run."""
    database_path = tmp_path / "agent-run.db"
    source_root = Path(__file__).parents[1]
    environment = os.environ | {
        "PYTHONPATH": str(source_root / "src"),
    }
    script = (
        "import time\n"
        "from agent_run.locking import acquire_run_lock\n"
        f"lock = acquire_run_lock({str(database_path)!r}, 'repository-id', 'build', run_id='0123456789abcdef0123456789abcdef')\n"
        "print(lock.run_id, flush=True)\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
    )

    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 5)
        assert ready
        holder_run_id = process.stdout.readline().strip()
        assert holder_run_id

        process.kill()
        process.wait(timeout=5)

        replacement_lock = acquire_run_lock(
            database_path,
            "repository-id",
            "build",
            run_id=uuid4().hex,
        )
        replacement_lock.release()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
