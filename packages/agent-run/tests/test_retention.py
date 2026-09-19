"""Tests for log retention selection and its preview command."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_run.cli import main
from agent_run.database import connect_database
from agent_run.retention import (
    MAX_LOG_BYTES,
    PruneCandidate,
    delete_prune_candidates,
    measure_log_sizes,
    select_prune_candidates,
)
from agent_run.runs import (
    RunRecord,
    create_run_log,
    get_run,
    resolve_log_directory,
    save_run,
)


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


def test_prune_apply_removes_the_runs_selected_by_preview(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Applying retention removes exactly the runs shown by its preview."""
    database_path = tmp_path / "agent-run.db"
    now = datetime.now(timezone.utc)
    connection = connect_database(database_path)

    try:
        old_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=now - timedelta(days=8),
            size=10,
        )
        recent_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=now - timedelta(days=1),
            size=20,
        )
    finally:
        connection.close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    preview_exit_code = main(["prune", "--json"])
    preview_output = capsys.readouterr()
    preview = json.loads(preview_output.out)

    apply_exit_code = main(["prune", "--apply", "--json"])
    apply_output = capsys.readouterr()
    applied = json.loads(apply_output.out)

    assert preview_exit_code == 0
    assert apply_exit_code == 0
    assert preview["data"]["applied"] is False
    assert applied["data"]["applied"] is True
    assert applied["data"]["runs"] == preview["data"]["runs"]
    assert applied["data"]["space_freed_bytes"] == preview["data"]["space_freed_bytes"]
    assert preview["data"]["total_log_bytes"] == 30
    assert applied["data"]["total_log_bytes"] == 20
    assert not (resolve_log_directory(database_path) / f"{old_id}.log").exists()
    assert (resolve_log_directory(database_path) / f"{recent_id}.log").exists()

    connection = connect_database(database_path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?", (old_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?", (recent_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_prune_apply_reports_removed_runs_in_text(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Text output identifies runs that retention removed."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)

    try:
        old_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=datetime.now(timezone.utc) - timedelta(days=8),
            size=10,
        )
    finally:
        connection.close()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["prune", "--apply"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "total log size: 0 bytes" in captured.out
    assert "Runs removed:" in captured.out
    assert f"run ID: {old_id}; reason: age; space freed: 10 bytes" in captured.out
    assert captured.err == ""


def test_prune_apply_tolerates_a_missing_log(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Applying retention removes a saved run even when its log is missing."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)

    try:
        run_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=datetime.now(timezone.utc) - timedelta(days=8),
            size=10,
        )
    finally:
        connection.close()

    log_path = resolve_log_directory(database_path) / f"{run_id}.log"
    log_path.unlink()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["prune", "--apply", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["data"]["applied"] is True
    assert result["data"]["runs"] == [
        {"run_id": run_id, "reason": "age", "space_freed_bytes": 0}
    ]

    connection = connect_database(database_path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_prune_apply_leaves_an_active_run_log_without_a_saved_record(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Applying retention leaves an active run log that has no saved record."""
    database_path = tmp_path / "agent-run.db"
    _, active_log_path = create_run_log(database_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["prune", "--apply", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["data"]["applied"] is True
    assert result["data"]["runs"] == []
    assert active_log_path.exists()


def test_prune_apply_keeps_the_record_when_unlink_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A failed log deletion reports its run and keeps the saved record."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)

    try:
        run_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=datetime.now(timezone.utc) - timedelta(days=8),
            size=10,
        )
    finally:
        connection.close()

    log_path = resolve_log_directory(database_path) / f"{run_id}.log"
    original_unlink = Path.unlink

    def fail_for_selected_log(path: Path, *args, **kwargs) -> None:
        """Refuse to delete the pruned run's log and delete any other path normally."""
        if path == log_path:
            raise PermissionError("log is locked")

        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_for_selected_log)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["prune", "--apply", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert result["error"]["code"] == "environment"
    assert run_id in result["error"]["message"]
    assert "log is locked" in result["error"]["message"]
    assert log_path.exists()

    connection = connect_database(database_path)
    try:
        assert get_run(connection, run_id).run_id == run_id
    finally:
        connection.close()


def test_prune_apply_reports_runs_removed_before_a_later_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """A failed later deletion reports runs removed earlier in the same apply."""
    database_path = tmp_path / "agent-run.db"
    now = datetime.now(timezone.utc)
    connection = connect_database(database_path)

    try:
        first_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=now - timedelta(days=10),
            size=10,
        )
        second_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=now - timedelta(days=9),
            size=20,
        )
        third_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=now - timedelta(days=8),
            size=30,
        )
    finally:
        connection.close()

    second_log_path = resolve_log_directory(database_path) / f"{second_id}.log"
    original_unlink = Path.unlink

    def fail_on_second_log(path: Path, *args, **kwargs) -> None:
        """Refuse to delete the second log while allowing other deletions."""
        if path == second_log_path:
            raise PermissionError("second log is locked")

        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_on_second_log)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    exit_code = main(["prune", "--apply", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert result["error"]["data"]["removed_runs"] == [first_id]
    assert first_id in result["error"]["message"]
    assert second_id in result["error"]["message"]
    assert third_id not in result["error"]["data"]["removed_runs"]

    connection = connect_database(database_path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?", (first_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?", (second_id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM runs WHERE run_id = ?", (third_id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_prune_apply_skips_a_record_removed_since_preview(tmp_path: Path) -> None:
    """A run record removed after preview is treated as already removed."""
    database_path = tmp_path / "agent-run.db"
    connection = connect_database(database_path)

    try:
        run_id = _save_run(
            connection,
            database_path,
            repository_id="repository",
            started_at=datetime.now(timezone.utc) - timedelta(days=8),
            size=10,
        )
        record = get_run(connection, run_id)
        connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

        removed = delete_prune_candidates(
            connection,
            [PruneCandidate(record=record, reason="age", size_bytes=10)],
        )
    finally:
        connection.close()

    assert removed == ()
    assert record.log_path.exists()
