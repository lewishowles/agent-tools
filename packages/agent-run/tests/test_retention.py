"""Tests for log retention selection and its preview command."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_run.cli import main
from agent_run.database import connect_database
from agent_run.retention import (
    MAX_LOG_BYTES,
    measure_log_sizes,
    select_prune_candidates,
)
from agent_run.runs import RunRecord, create_run_log, resolve_log_directory, save_run


def _record(run_id: str, started_at: datetime, log_path: Path) -> RunRecord:
    """Build a saved-run value for selection tests."""
    return RunRecord(
        run_id=run_id,
        repository_id=f"repository-{run_id}",
        argv=("check",),
        working_directory=".",
        timeout_seconds=5,
        started_at=started_at.isoformat(),
        duration_seconds=0.1,
        exit_status=0,
        timed_out=False,
        log_path=log_path,
    )


def test_selection_uses_age_then_oldest_runs_for_the_size_limit(tmp_path: Path) -> None:
    """Age candidates are selected first, then the oldest runs needed by size."""
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    old = _record("old", now - timedelta(days=8), tmp_path / "old.log")
    first_recent = _record(
        "first-recent", now - timedelta(days=2), tmp_path / "first-recent.log"
    )
    second_recent = _record(
        "second-recent", now - timedelta(days=1), tmp_path / "second-recent.log"
    )
    sizes = {
        old.run_id: 10,
        first_recent.run_id: MAX_LOG_BYTES + 5,
        second_recent.run_id: 1,
    }

    plan = select_prune_candidates(
        [second_recent, old, first_recent],
        now=now,
        sizes=sizes,
        total_bytes=MAX_LOG_BYTES + 16,
    )

    assert [(item.record.run_id, item.reason) for item in plan.candidates] == [
        ("old", "age"),
        ("first-recent", "size"),
    ]
    assert plan.total_bytes == MAX_LOG_BYTES + 16
    assert plan.freed_bytes == MAX_LOG_BYTES + 15


def test_run_exactly_seven_days_old_is_kept(tmp_path: Path) -> None:
    """A run at the seven-day boundary is not old enough to remove."""
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    boundary = _record("boundary", now - timedelta(days=7), tmp_path / "boundary.log")

    plan = select_prune_candidates(
        [boundary],
        now=now,
        sizes={boundary.run_id: 1},
        total_bytes=1,
    )

    assert plan.candidates == ()


def test_run_one_second_past_seven_days_is_selected(tmp_path: Path) -> None:
    """A run one second past the seven-day boundary is selected by age."""
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    old = _record(
        "one-second-old",
        now - timedelta(days=7, seconds=1),
        tmp_path / "one-second-old.log",
    )

    plan = select_prune_candidates(
        [old],
        now=now,
        sizes={old.run_id: 1},
        total_bytes=1,
    )

    assert [item.record.run_id for item in plan.candidates] == [old.run_id]
    assert plan.candidates[0].reason == "age"


def test_large_orphan_log_alone_does_not_trigger_size_selection(
    tmp_path: Path,
) -> None:
    """An orphaned log does not trigger size selection for a saved run."""
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    saved = _record("saved", now - timedelta(days=1), tmp_path / "saved.log")

    plan = select_prune_candidates(
        [saved],
        now=now,
        sizes={saved.run_id: 1},
        total_bytes=MAX_LOG_BYTES + 1,
    )

    assert plan.candidates == ()
    assert plan.freed_bytes == 0


def test_measure_log_sizes_ignores_saved_logs_outside_the_shared_directory(
    tmp_path: Path,
) -> None:
    """A saved log outside the shared directory cannot inflate freed space."""
    log_directory = tmp_path / "agent-run-logs"
    log_directory.mkdir()
    inside_path = log_directory / "inside.log"
    outside_path = tmp_path / "outside.log"
    inside_path.write_bytes(b"inside")
    outside_path.write_bytes(b"outside")

    inside = _record("inside", datetime.now(timezone.utc), inside_path)
    outside = _record("outside", datetime.now(timezone.utc), outside_path)

    total_bytes, sizes = measure_log_sizes(log_directory, [inside, outside])

    assert total_bytes == inside_path.stat().st_size
    assert sizes == {"inside": inside_path.stat().st_size}


def _save_run(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    repository_id: str,
    started_at: datetime,
    size: int,
) -> str:
    """Create one saved run and write the requested number of log bytes."""
    run_id, log_path = create_run_log(database_path)
    log_path.write_bytes(b"x" * size)
    save_run(
        connection,
        run_id=run_id,
        repository_id=repository_id,
        argv=("check",),
        working_directory=".",
        timeout_seconds=5,
        started_at=started_at.isoformat(),
        duration_seconds=0.1,
        exit_status=0,
        timed_out=False,
        log_path=log_path,
    )
    return run_id


def test_prune_previews_all_repositories_and_reports_shared_log_totals(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """The CLI previews saved runs from every repository and excludes orphans."""
    database_path = tmp_path / "agent-run.db"
    now = datetime.now(timezone.utc)
    connection = connect_database(database_path)

    try:
        old_id = _save_run(
            connection,
            database_path,
            repository_id="first-repository",
            started_at=now - timedelta(days=8),
            size=10,
        )
        recent_id = _save_run(
            connection,
            database_path,
            repository_id="second-repository",
            started_at=now - timedelta(days=1),
            size=20,
        )
    finally:
        connection.close()

    orphan_path = resolve_log_directory(database_path) / "orphan.log"
    with orphan_path.open("wb") as orphan:
        orphan.truncate(MAX_LOG_BYTES)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["prune", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["data"]["total_log_bytes"] == MAX_LOG_BYTES + 30
    assert result["data"]["space_freed_bytes"] == 10
    assert result["data"]["runs"] == [
        {
            "run_id": old_id,
            "reason": "age",
            "space_freed_bytes": 10,
        },
    ]
    assert "orphan" not in captured.out

    text_exit_code = main(["prune"])
    text_output = capsys.readouterr()

    assert text_exit_code == 0
    assert f"total log size: {MAX_LOG_BYTES + 30} bytes" in text_output.out
    assert "space freed: 10 bytes" in text_output.out
    assert f"run ID: {old_id}; reason: age" in text_output.out
    assert recent_id not in text_output.out
    assert "orphan" not in text_output.out
