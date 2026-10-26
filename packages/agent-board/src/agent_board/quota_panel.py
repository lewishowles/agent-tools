"""Draw the quota panel: each account's usage, reset times and reading age."""

from datetime import datetime, timezone

from agent_board.board import MAX_FRAME_WIDTH, shorten_visible
from agent_board.quota import AccountReading, QuotaSnapshot


def render_quota_panel(
    snapshot: QuotaSnapshot, *, width: int, now: datetime | None = None
) -> list[str]:
    """Show each account's usage, reset time and reading age within the board width.

    Args:
        snapshot: The latest readings and state of the background check.
        width: The available terminal columns.
        now: The time to measure reset times and reading ages from. Defaults
            to the system clock.

    Returns:
        The panel lines, without the blank line that separates it from teams.
    """
    # The panel follows the teams frame's 80-column cap while stacked below it.
    panel_width = min(width, MAX_FRAME_WIDTH)
    current_time = now or datetime.now(timezone.utc)
    heading = "Quota · Checking…" if snapshot.checking else "Quota"
    lines = [heading]

    if not snapshot.accounts and snapshot.error is None:
        lines.append("Not checked yet")

    for reading in sorted(
        snapshot.accounts.values(),
        key=lambda account: (account.provider, account.account),
    ):
        lines.extend(_account_lines(reading, current_time))

    if snapshot.error is not None:
        lines.append(f"Error: {_error_message(snapshot.error)}")

    return [shorten_visible(line, panel_width) for line in lines]


def _account_lines(reading: AccountReading, now: datetime) -> list[str]:
    """Give one account its own short block: name, usage, reset times and age."""
    account_name = f"{reading.provider.capitalize()} · {reading.account}"
    checked = _reading_age(reading.checked_at, now)

    if reading.error is not None:
        return [
            account_name,
            f"  Error: {' '.join(reading.error.split())}",
            f"  {checked}",
        ]

    session_reset = _time_left(reading.session.resets_at, now)
    week_reset = _time_left(reading.week.resets_at, now)
    return [
        account_name,
        f"  session {reading.session.used_percent}% · week {reading.week.used_percent}%",
        f"  resets {session_reset} / {week_reset}",
        f"  {checked}",
    ]


def _time_left(resets_at: str | None, now: datetime) -> str:
    """Say how long is left until a reset, such as "2h 10m".

    Returns "now" once the reset has passed, or "unknown" when the time is
    missing, unreadable or has no time zone.
    """
    if resets_at is None:
        return "unknown"

    try:
        reset_time = datetime.fromisoformat(resets_at)
    except ValueError:
        return "unknown"

    if reset_time.tzinfo is None:
        return "unknown"

    seconds = max(0, int((reset_time - now).total_seconds()))
    if seconds == 0:
        return "now"

    minutes = (seconds + 59) // 60
    days, remaining_minutes = divmod(minutes, 1440)
    hours, minutes = divmod(remaining_minutes, 60)

    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"

    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"

    return f"{minutes}m"


def _reading_age(checked_at: str | None, now: datetime) -> str:
    """Say how long ago an account was checked, rounded down, such as "checked 3m ago".

    Returns "check time unknown" when the time is missing, unreadable or has no
    time zone.
    """
    if checked_at is None:
        return "check time unknown"

    try:
        checked_time = datetime.fromisoformat(checked_at)
    except ValueError:
        return "check time unknown"

    if checked_time.tzinfo is None:
        return "check time unknown"

    seconds = max(0, int((now - checked_time).total_seconds()))
    if seconds < 60:
        return "checked just now"

    minutes = seconds // 60
    if minutes < 60:
        return f"checked {minutes}m ago"

    hours = minutes // 60
    if hours < 24:
        return f"checked {hours}h ago"

    return f"checked {hours // 24}d ago"


def _error_message(error: str) -> str:
    """Explain a missing agent-quota in plain words and fold other errors onto one line."""
    if "No such file or directory: 'agent-quota'" in error:
        return "agent-quota isn't installed, so quota can't be checked"

    return " ".join(error.split())
