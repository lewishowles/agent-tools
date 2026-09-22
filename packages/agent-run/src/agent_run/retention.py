"""Choose which saved runs the log retention policy would remove."""

import os
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_run.runs import RUNNING_STATUS, RunRecord

# A saved run becomes eligible by age after this many days.
RETENTION_DAYS = 7
# The total size allowed for saved-run logs that retention can remove.
MAX_LOG_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class PruneCandidate:
    """One saved run selected by the retention policy."""

    record: RunRecord
    reason: str
    size_bytes: int


@dataclass(frozen=True)
class LogPruneCandidate:
    """One old log with no saved run record selected by the retention policy."""

    path: Path
    size_bytes: int


@dataclass(frozen=True)
class RetentionPlan:
    """The runs and logs the retention policy would remove, with size totals."""

    candidates: tuple[PruneCandidate, ...]
    logs: tuple[LogPruneCandidate, ...]
    total_bytes: int
    freed_bytes: int


class PruneError(RuntimeError):
    """Report a run or log that could not be removed during pruning."""

    def __init__(
        self,
        target: str,
        reason: str,
        removed: Sequence[PruneCandidate],
        removed_logs: Sequence[LogPruneCandidate] = (),
        *,
        target_kind: str = "run",
    ) -> None:
        """Store the failed target and items removed before it."""
        self.target = target
        self.reason = reason
        self.removed = tuple(removed)
        self.removed_logs = tuple(removed_logs)
        self.target_kind = target_kind
        super().__init__(f'Could not prune {target_kind} "{target}": {reason}')


def measure_log_sizes(
    log_directory: str | Path,
    records: Sequence[RunRecord],
    *,
    now: datetime | None = None,
) -> tuple[int, dict[str, int], tuple[LogPruneCandidate, ...]]:
    """Return the directory total, saved-run log sizes, and old orphan logs.

    The total counts every ``.log`` file in the directory, including logs with
    no saved run, so the preview shows how much space logs really use. The
    per-run sizes only include saved runs whose log is inside the directory.

    A log with no saved run is only returned for removal once nothing has
    written to it for ``RETENTION_DAYS``. The run record is saved when the
    command finishes, so a newer log may belong to a run that is still going.
    ``now`` defaults to the current time.
    """
    directory = Path(log_directory).expanduser().resolve()
    sizes: dict[str, int] = {}
    saved_paths: set[Path] = set()

    for record in records:
        log_path = record.log_path.expanduser().resolve()

        try:
            log_path.relative_to(directory)
        except ValueError:
            continue

        saved_paths.add(log_path)

        try:
            sizes[record.run_id] = log_path.stat().st_size
        except OSError:
            continue

    current_time = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    cutoff = current_time - timedelta(days=RETENTION_DAYS)
    total_bytes = 0
    orphan_logs: list[LogPruneCandidate] = []

    if directory.is_dir():
        for path in sorted(directory.glob("*.log")):
            try:
                if not path.is_file():
                    continue

                file_stat = path.stat()
                total_bytes += file_stat.st_size

                resolved_path = path.resolve()
                modified_at = datetime.fromtimestamp(
                    file_stat.st_mtime, tz=timezone.utc
                )
            except OSError:
                continue

            if resolved_path in saved_paths or modified_at >= cutoff:
                continue

            orphan_logs.append(
                LogPruneCandidate(path=resolved_path, size_bytes=file_stat.st_size)
            )

    return total_bytes, sizes, tuple(orphan_logs)


def _is_process_alive(pid: int) -> bool:
    """Report whether the agent-run process that owns a running record still exists.

    A permission error means the process exists but belongs to another user, so
    it counts as alive. Treating it as gone would delete a run someone else owns.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def select_prune_candidates(
    records: Sequence[RunRecord],
    *,
    now: datetime,
    sizes: Mapping[str, int],
    total_bytes: int,
    orphan_logs: Sequence[LogPruneCandidate] = (),
    liveness_check: Callable[[int], bool] = _is_process_alive,
) -> RetentionPlan:
    """Select abandoned runs, then old runs and runs needed to meet the size limit.

    The size limit is measured against the saved-run sizes in ``sizes`` only.
    Orphan logs are already selected separately by ``measure_log_sizes`` and
    are passed through to the plan for reporting and deletion. A live running
    run remains in the size total but is never selected by age or size.
    """
    current_time = _as_utc(now)
    cutoff = current_time - timedelta(days=RETENTION_DAYS)
    ordered_records = sorted(records, key=_record_sort_key)
    selected: list[PruneCandidate] = []
    selected_ids: set[str] = set()
    # Runs whose agent-run process is still going, which prune must leave alone.
    live_running_ids = {
        record.run_id
        for record in ordered_records
        if (
            record.status == RUNNING_STATUS
            and record.pid is not None
            and liveness_check(record.pid)
        )
    }
    freed_bytes = 0

    for record in ordered_records:
        if record.status != RUNNING_STATUS or record.run_id in live_running_ids:
            continue

        size_bytes = _size_for_record(record, sizes)
        selected.append(
            PruneCandidate(record=record, reason="abandoned", size_bytes=size_bytes)
        )
        selected_ids.add(record.run_id)
        freed_bytes += size_bytes

    for record in ordered_records:
        if record.run_id in selected_ids or record.run_id in live_running_ids:
            continue

        if _started_at(record) < cutoff:
            size_bytes = _size_for_record(record, sizes)
            selected.append(
                PruneCandidate(record=record, reason="age", size_bytes=size_bytes)
            )
            selected_ids.add(record.run_id)
            freed_bytes += size_bytes

    saved_bytes = sum(_size_for_record(record, sizes) for record in ordered_records)
    remaining_bytes = saved_bytes - freed_bytes

    if remaining_bytes > MAX_LOG_BYTES:
        for record in ordered_records:
            if record.run_id in selected_ids or record.run_id in live_running_ids:
                continue

            size_bytes = _size_for_record(record, sizes)
            selected.append(
                PruneCandidate(record=record, reason="size", size_bytes=size_bytes)
            )
            selected_ids.add(record.run_id)
            freed_bytes += size_bytes
            remaining_bytes -= size_bytes

            if remaining_bytes <= MAX_LOG_BYTES:
                break

    return RetentionPlan(
        candidates=tuple(selected),
        logs=tuple(orphan_logs),
        total_bytes=total_bytes,
        freed_bytes=freed_bytes + sum(log.size_bytes for log in orphan_logs),
    )


def delete_prune_candidates(
    connection: sqlite3.Connection,
    candidates: Sequence[PruneCandidate],
    logs: Sequence[LogPruneCandidate] = (),
) -> tuple[PruneCandidate, ...]:
    """Delete each chosen run and orphan log, stopping at the first failure.

    Each run is deleted in its own transaction. The record is only committed as
    deleted once its log file is gone, so a log that cannot be removed keeps its
    record and the next prune can try again. A log or record that is already
    missing does not count as a failure. Orphan logs are deleted after the saved
    runs because they have no database record to update.

    Args:
        connection: Open database connection used to remove run records.
        candidates: Runs selected by ``select_prune_candidates``.
        logs: Orphan logs selected by ``measure_log_sizes``.

    Returns:
        The runs removed during this call, in selection order.

    Raises:
        PruneError: If a run record or log cannot be removed. The error keeps
            the runs and orphan logs removed before the failure.
    """
    removed: list[PruneCandidate] = []

    for candidate in candidates:
        run_id = candidate.record.run_id

        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM runs WHERE run_id = ?",
                (run_id,),
            ).rowcount

            if deleted == 0:
                connection.commit()
                continue

            try:
                candidate.record.log_path.unlink()
            except FileNotFoundError:
                pass

            connection.commit()
        except (OSError, sqlite3.Error) as error:
            if connection.in_transaction:
                connection.rollback()

            raise PruneError(run_id, str(error), removed) from error

        removed.append(candidate)

    removed_logs: list[LogPruneCandidate] = []

    for candidate in logs:
        try:
            candidate.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise PruneError(
                str(candidate.path),
                str(error),
                removed,
                removed_logs,
                target_kind="log",
            ) from error

        removed_logs.append(candidate)

    return tuple(removed)


def _as_utc(value: datetime) -> datetime:
    """Convert a datetime to UTC, treating one without a time zone as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _started_at(record: RunRecord) -> datetime:
    """Return when a saved run started, in UTC."""
    return _as_utc(datetime.fromisoformat(record.started_at))


def _record_sort_key(record: RunRecord) -> tuple[datetime, str]:
    """Sort saved runs oldest first with a deterministic tie-breaker."""
    return _started_at(record), record.run_id


def _size_for_record(record: RunRecord, sizes: Mapping[str, int]) -> int:
    """Return the size of a run's log, or zero when it could not be measured."""
    return sizes.get(record.run_id, 0)
