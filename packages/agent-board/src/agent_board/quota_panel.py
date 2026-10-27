"""Draw account quota as a boxed table beside or below the teams board."""

from datetime import datetime, timezone
from enum import Enum

from agent_board.board import (
    AMBER_STYLE,
    DIM_STYLE,
    MAX_FRAME_WIDTH,
    RESET_STYLE,
    frame_lines,
)
from agent_board.quota import AccountReading, QuotaSnapshot, QuotaWindow

# The columns the frame takes from the box width: a border and two spaces on
# each side.
FRAME_PADDING = 6

# The number of cells in a usage bar, so each cell stands for 10%.
BAR_CELLS = 10

# The width of a usage percentage such as " 42%" or "100%".
PERCENT_WIDTH = 4

# The width of a reset time such as "  ↻  mon 14:05".
RESET_LABEL_WIDTH = 14

# The space between a usage bar and its percentage.
BAR_PERCENT_GAP = 1

# The space between the account label and the session window.
LABEL_GAP = 2

# The space between the session and week windows.
WINDOW_GAP = 4

# The spaces written between the account label and the session window.
LABEL_SEPARATOR = " " * LABEL_GAP

# The spaces written between the session and week windows.
WINDOW_SEPARATOR = " " * WINDOW_GAP

# The width of one window shown as a bar and percentage.
BAR_WINDOW_WIDTH = BAR_CELLS + BAR_PERCENT_GAP + PERCENT_WIDTH

# The width of one window shown with its bar, percentage and reset time.
FULL_WINDOW_WIDTH = BAR_WINDOW_WIDTH + RESET_LABEL_WIDTH

# The room a row needs after the account label to show both windows in full.
FULL_ROW_WIDTH = LABEL_GAP + FULL_WINDOW_WIDTH * 2 + WINDOW_GAP

# The room a row needs after the account label to show both bars without reset times.
BAR_ROW_WIDTH = LABEL_GAP + BAR_WINDOW_WIDTH * 2 + WINDOW_GAP

# The colour of the ┃ marker that shows how far through the window the clock is.
PACE_MARKER_RGB = (235, 235, 245)

# The grey for empty bar cells and reset times, matching agent-quota's table.
DIM_BAR_RGB = (110, 110, 125)


class WindowMode(str, Enum):
    """How much of each quota window a row shows, from the most detail to the least."""

    FULL = "full"
    BARS = "bars"
    PERCENT = "percent"


def render_quota_panel(
    snapshot: QuotaSnapshot,
    *,
    width: int,
    now: datetime | None = None,
    colour: bool = False,
) -> list[str]:
    """Show one row per account inside the board's rounded frame.

    Args:
        snapshot: The latest readings and state of the background check.
        width: The available terminal columns.
        now: The time to measure pace and reading age from. Defaults to the
            system clock.
        colour: Whether the board is using terminal colour.

    Returns:
        The quota box lines, without the blank line that separates the box
        from the teams.
    """
    # The box width, capped to match the teams frame.
    panel_width = min(width, MAX_FRAME_WIDTH)
    # The moment that pace and reading age are measured from.
    current_time = now or datetime.now(timezone.utc)
    # The accounts sorted by provider, then account, so rows keep their order.
    accounts = sorted(
        snapshot.accounts.values(),
        key=lambda reading: (reading.provider, reading.account),
    )

    # The rows shown inside the box: the account table, any error, or "checking…".
    lines = _build_rows(snapshot, accounts, panel_width, current_time, colour)
    # The reading age or check status for the box's top-right edge.
    age = _age_text(snapshot, accounts, current_time)

    # Drop the frame's leading blank line; the board adds its own when it stacks
    # the box below the teams.
    return frame_lines(
        lines,
        panel_width,
        age,
        None,
        frame_style=AMBER_STYLE if colour else None,
        title="Quota",
    )[1:]


def _build_rows(
    snapshot: QuotaSnapshot,
    accounts: list[AccountReading],
    panel_width: int,
    current_time: datetime,
    colour: bool,
) -> list[str]:
    """Build the box content: a row per account, then any whole-check error.

    The column heading appears above the account rows only when there are
    accounts to show. Before the first result arrives, the box shows
    "checking…" instead.
    """
    # The table label for each account, in row order.
    labels = [_account_label(reading) for reading in accounts]
    # The label column width, the level of detail, and each window column's width.
    label_width, mode, window_width = _layout(labels, panel_width - FRAME_PADDING)
    # The column headings. The percentages-only layout uses shorter ones that
    # fit over its four-character columns.
    heading = (
        f"{'':{label_width}}{LABEL_SEPARATOR}"
        f"{'5h':>{window_width}}{WINDOW_SEPARATOR}{'week':>{window_width}}"
        if mode == WindowMode.PERCENT
        else f"{'':{label_width}}{LABEL_SEPARATOR}"
        f"{'5h used':{window_width}}{WINDOW_SEPARATOR}week used"
    )
    # The rows shown inside the box, from top to bottom.
    lines = []

    if accounts:
        lines.append(f"{DIM_STYLE}{heading}{RESET_STYLE}" if colour else heading)

    if not accounts and snapshot.error is None:
        lines.append("checking…")

    for reading, label in zip(accounts, labels):
        if reading.error is not None:
            lines.append(
                f"{label:<{label_width}}{LABEL_SEPARATOR}"
                f"unavailable: {' '.join(reading.error.split())}"
            )
            continue

        if reading.session is None or reading.week is None:
            lines.append(
                f"{label:<{label_width}}{LABEL_SEPARATOR}unavailable: missing quota window"
            )
            continue

        # The session and week windows, drawn at the same level of detail.
        session = _render_window(reading.session, current_time, colour, mode)
        week = _render_window(reading.week, current_time, colour, mode)
        lines.append(
            f"{label:<{label_width}}{LABEL_SEPARATOR}{session}{WINDOW_SEPARATOR}{week}"
        )

    if snapshot.error is not None:
        lines.append(f"Error: {_error_message(snapshot.error)}")

    return lines


def _age_text(
    snapshot: QuotaSnapshot, accounts: list[AccountReading], current_time: datetime
) -> str:
    """Choose the status shown in the box's top edge.

    The status is "checking…" while a check runs or before the first result
    arrives. Otherwise it says how long ago the newest dated check happened, or
    is empty when the finished check gave no time, as when every account failed
    or the whole check failed.
    """
    if snapshot.checking:
        return "checking…"

    # The check times that could be read; the newest one sets the age.
    checked_times = [
        checked
        for reading in accounts
        if (checked := _parse_time(reading.checked_at)) is not None
    ]
    if checked_times:
        return _reading_age(max(checked_times), current_time)

    return "checking…" if not accounts and snapshot.error is None else ""


def _account_label(reading: AccountReading) -> str:
    """Label an account by its provider, adding " (2)" for the second account."""
    return reading.provider + (" (2)" if reading.account == "2" else "")


def _layout(labels: list[str], content_width: int) -> tuple[int, WindowMode, int]:
    """Choose the most detailed row layout that fits inside the box.

    Reset times go first, then bars. The frame shortens any row that still does
    not fit.

    Returns:
        The label column width, the layout, and the width of one window column.
    """
    # The longest account name sets the width of the label column.
    longest_label = max((len(label) for label in labels), default=0)

    if longest_label + FULL_ROW_WIDTH <= content_width:
        return longest_label, WindowMode.FULL, FULL_WINDOW_WIDTH

    if longest_label + BAR_ROW_WIDTH <= content_width:
        return longest_label, WindowMode.BARS, BAR_WINDOW_WIDTH

    return longest_label, WindowMode.PERCENT, PERCENT_WIDTH


def _render_window(
    window: QuotaWindow, now: datetime, colour: bool, mode: WindowMode
) -> str:
    """Draw one session or week window at the layout's level of detail."""
    # The reset time, how far through the window the clock is, and the pace colour.
    reset_time, clock, pace_rgb = _window_pace(window, now)
    # The usage as a whole percentage, padded so the columns line up.
    percentage = _rgb(
        *pace_rgb, f"{int(window.used_percent + 0.5)}%".rjust(PERCENT_WIDTH), colour
    )

    if mode == WindowMode.PERCENT:
        return percentage

    # The usage bar followed by the percentage.
    window_text = (
        f"{_draw_bar(window.used_percent, clock, pace_rgb, colour)} {percentage}"
    )

    if mode == WindowMode.BARS:
        return window_text

    return window_text + _reset_label(reset_time, colour)


def _window_pace(
    window: QuotaWindow, now: datetime
) -> tuple[datetime | None, float | None, tuple[float, float, float]]:
    """Work out where a window stands against its reset.

    Returns:
        The reset time, how far through the window the clock is as a
        percentage, and the colour for usage against that pace. The reset time
        and clock are None when the reset time is unknown, and the colour then
        follows usage alone.
    """
    # The share of the window's quota already used.
    used = window.used_percent
    # The reset time, or None when it is missing or unreadable.
    reset_time = _parse_time(window.resets_at)
    # How far through the window the clock is, as a percentage, when known.
    clock = None

    if reset_time is not None and window.window_seconds > 0:
        # The seconds left until the reset, or zero once it has passed.
        remaining = max((reset_time - now).total_seconds(), 0)
        # The seconds since the window started, kept within its length.
        elapsed = max(0, min(window.window_seconds, window.window_seconds - remaining))
        clock = elapsed / window.window_seconds * 100

    # The pace score for the colour, from usage alone when the clock is unknown.
    tone = (
        max(0, min(used, 100)) / 100
        if clock is None
        else max(0, min(0.5 - (clock - used) / 50, 1))
    )
    return reset_time, clock, _pace_colour(tone)


def _draw_bar(
    used: float,
    clock: float | None,
    pace_rgb: tuple[float, float, float],
    colour: bool,
) -> str:
    """Draw the usage bar, with ┃ marking how far through the window the clock is."""
    # The number of filled cells, rounded to the nearest cell.
    filled = int(max(0, min(used, 100)) * BAR_CELLS / 100 + 0.5)
    # The cell that shows the clock, kept in the last cell at the reset.
    marker = (
        None
        if clock is None
        else max(0, min(int(clock * BAR_CELLS / 100), BAR_CELLS - 1))
    )
    # The drawn cells. The clock marker replaces a filled or empty cell.
    cells = []

    for index in range(BAR_CELLS):
        if index == marker:
            cells.append(_rgb(*PACE_MARKER_RGB, "┃", colour))
        elif index < filled:
            cells.append(_rgb(*pace_rgb, "█", colour))
        else:
            cells.append(_rgb(*DIM_BAR_RGB, "░", colour))

    return "".join(cells)


def _reset_label(reset_time: datetime | None, colour: bool) -> str:
    """Show the local reset time, or the same width of blank space when unknown."""
    if reset_time is None:
        return " " * RESET_LABEL_WIDTH

    return _rgb(
        *DIM_BAR_RGB,
        f"  ↻  {reset_time.astimezone().strftime('%a %H:%M').lower()}",
        colour,
    )


def _pace_colour(tone: float) -> tuple[float, float, float]:
    """Turn a pace score into agent-quota's green-to-red colour.

    A score of 0.5 means usage matches the clock. Lower scores, where usage is
    behind the clock, move towards green; higher scores move through amber to
    red.
    """
    if tone < 0.5:
        return 90 + tone * 2 * 155, 220 - tone * 2 * 30, 160 - tone * 2 * 90

    return 245, 190 - (tone - 0.5) * 2 * 110, 70 + (tone - 0.5) * 2 * 30


def _rgb(red: float, green: float, blue: float, value: str, colour: bool) -> str:
    """Colour the text with an RGB terminal colour when the board uses colour."""
    if not colour:
        return value

    return f"\x1b[38;2;{int(red)};{int(green)};{int(blue)}m{value}{RESET_STYLE}"


def _parse_time(value: str | None) -> datetime | None:
    """Read an ISO timestamp, or None when missing, unreadable or without a zone."""
    if value is None:
        return None

    try:
        # The timestamp as written, which may lack a time zone.
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    # Only timestamps with a time zone can be compared with the clock.
    return parsed if parsed.tzinfo is not None else None


def _reading_age(checked_at: datetime, now: datetime) -> str:
    """Say how long ago a reading was taken, rounded down, such as "checked 3m ago"."""
    # A future reading is treated as having just arrived.
    seconds = max(0, int((now - checked_at).total_seconds()))
    if seconds < 60:
        return "checked just now"

    # The whole minutes since the reading, shown for ages under an hour.
    minutes = seconds // 60
    if minutes < 60:
        return f"checked {minutes}m ago"

    # The whole hours since the reading, shown for ages under a day.
    hours = minutes // 60
    if hours < 24:
        return f"checked {hours}h ago"

    return f"checked {hours // 24}d ago"


def _error_message(error: str) -> str:
    """Explain a missing agent-quota and fold other errors onto one line."""
    if "No such file or directory: 'agent-quota'" in error:
        return "agent-quota isn't installed, so quota can't be checked"

    return " ".join(error.split())
