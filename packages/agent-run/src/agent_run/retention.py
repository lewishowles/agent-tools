"""Choose which saved runs the log retention policy would remove."""

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
