"""Choose which saved runs the log retention policy would remove."""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_run.runs import RunRecord

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
class RetentionPlan:
    """The runs the retention policy would remove, with the log size totals."""

    candidates: tuple[PruneCandidate, ...]
    total_bytes: int
    freed_bytes: int


class PruneError(RuntimeError):
    """Report a run that could not be removed during retention pruning."""

    def __init__(
        self,
        run_id: str,
        reason: str,
        removed: Sequence[PruneCandidate],
    ) -> None:
        """Store the failed run, its reason, and runs removed before it."""
        self.run_id = run_id
        self.reason = reason
        self.removed = tuple(removed)
        super().__init__(f'Could not prune run "{run_id}": {reason}')


def measure_log_sizes(
    log_directory: str | Path,
    records: Sequence[RunRecord],
) -> tuple[int, dict[str, int]]:
    """Return the shared directory size and the size of each saved run log.

    The total counts every ``.log`` file in the directory, including logs with
    no saved run, so the preview shows how much space logs really use. The
    per-run sizes only include saved runs whose log is inside the directory,
    because those are the only logs pruning can remove.
    """
    directory = Path(log_directory).expanduser().resolve()
    total_bytes = 0

    if directory.is_dir():
        for path in directory.glob("*.log"):
            try:
                if path.is_file():
                    total_bytes += path.stat().st_size
            except OSError:
                continue

    sizes: dict[str, int] = {}

    for record in records:
        log_path = record.log_path.expanduser().resolve()

        try:
            log_path.relative_to(directory)
        except ValueError:
            continue

        try:
            sizes[record.run_id] = log_path.stat().st_size
        except OSError:
            continue

    return total_bytes, sizes


def select_prune_candidates(
    records: Sequence[RunRecord],
    *,
    now: datetime,
    sizes: Mapping[str, int],
    total_bytes: int,
) -> RetentionPlan:
    """Select old runs, then oldest runs needed to meet the size limit.

    The size limit is measured against the saved-run sizes in ``sizes`` only.
    Logs with no saved run cannot be removed, so counting them could select
    every run without ever getting under the limit. ``total_bytes`` is passed
    through to the plan for reporting.
    """
    current_time = _as_utc(now)
    cutoff = current_time - timedelta(days=RETENTION_DAYS)
    ordered_records = sorted(records, key=_record_sort_key)
    selected: list[PruneCandidate] = []
    selected_ids: set[str] = set()
    freed_bytes = 0

    for record in ordered_records:
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
            if record.run_id in selected_ids:
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
        total_bytes=total_bytes,
        freed_bytes=freed_bytes,
    )


def delete_prune_candidates(
    connection: sqlite3.Connection,
    candidates: Sequence[PruneCandidate],
) -> tuple[PruneCandidate, ...]:
    """Delete each chosen run's saved record and log, stopping at the first failure.

    Each run is deleted in its own transaction. The record is only committed as
    deleted once its log file is gone, so a log that cannot be removed keeps its
    record and the next prune can try again. A log or record that is already
    missing does not count as a failure.

    Args:
        connection: Open database connection used to remove run records.
        candidates: Runs selected by ``select_prune_candidates``.

    Returns:
        The runs removed during this call, in selection order.

    Raises:
        PruneError: If a run record or log cannot be removed. The error keeps
            the runs removed before the failure.
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
