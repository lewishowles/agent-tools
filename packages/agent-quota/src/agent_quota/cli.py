"""Show each account's Claude or Codex quota as a table or as JSON."""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from agent_quota import probe

# The four accounts shown by the command, in their display order.
ACCOUNTS = (
    ("claude", "default"),
    ("claude", "2"),
    ("codex", "default"),
    ("codex", "2"),
)


def iso_time(timestamp):
    """Format a Unix timestamp as an ISO 8601 time in UTC."""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def account_data(reading):
    """Turn one probe reading into its JSON entry, with used rather than remaining percentages.

    Used percentages are rounded to whole numbers, as the table shows them, so every
    provider gives the same number type.
    """
    result = {
        "provider": reading["provider"],
        "account": reading["account"],
        "checked_at": iso_time(reading["checked_at"])
        if "checked_at" in reading
        else None,
    }

    if "error" in reading:
        result["error"] = reading["error"]
        return result

    for name in ("session", "week"):
        window = reading[name]
        result[name] = {
            "used_percent": int(100 - window["remaining_percent"] + 0.5),
            "resets_at": iso_time(window["resets_at"]) if window["resets_at"] else None,
            "window_seconds": window["window_seconds"],
        }

    return result


def clamp(value, minimum, maximum):
    """Keep a bar position or percentage within its visible range."""
    return max(minimum, min(maximum, value))


def rgb(red, green, blue, value, colour):
    """Colour the text with a true-colour terminal style, or return it unchanged when colour is off.

    @param  {int|float}  red
        Red channel from 0 to 255.
    @param  {int|float}  green
        Green channel from 0 to 255.
    @param  {int|float}  blue
        Blue channel from 0 to 255.
    @param  {str}  value
        The text to colour.
    @param  {bool}  colour
        Whether to add terminal colour codes.
    """
    if not colour:
        return value

    return f"\033[38;2;{int(red)};{int(green)};{int(blue)}m{value}\033[0m"


def pace_colour(tone):
    """Pick the bar colour for a tone from 0 (green, well under pace) to 1 (red, well over pace)."""
    if tone < 0.5:
        return (90 + tone * 2 * 155, 220 - tone * 2 * 30, 160 - tone * 2 * 90)

    return (245, 190 - (tone - 0.5) * 2 * 110, 70 + (tone - 0.5) * 2 * 30)


def render_window(window, now, colour):
    """Draw one window's bar, used percentage and local reset time.

    Filled cells show the quota used and the ┃ marker shows how far through the
    window the clock is. Without a reset time there is no marker, and the colour
    follows the quota used alone.

    @param  {dict}  window
        One window from an account_data entry, with used_percent, resets_at as
        an ISO string or None, and window_seconds. A raw probe window, which
        holds remaining_percent and a Unix resets_at, does not work here.
    @param  {int|float}  now
        Current time in Unix seconds.
    @param  {bool}  colour
        Whether to add terminal colour codes.
    """
    used = window["used_percent"]
    reset = window["resets_at"]
    duration = window["window_seconds"]

    if reset is None:
        clock = None
        reset_text = ""
    else:
        reset_epoch = datetime.fromisoformat(reset).timestamp()
        remaining = max(reset_epoch - now, 0)
        elapsed = clamp(duration - remaining, 0, duration)
        clock = elapsed / duration * 100
        reset_text = (
            datetime.fromtimestamp(reset_epoch, timezone.utc)
            .astimezone()
            .strftime("%a %H:%M")
            .lower()
        )

    tone = (
        clamp(used, 0, 100) / 100
        if clock is None
        else clamp(0.5 - (clock - used) / 50, 0, 1)
    )
    red, green, blue = pace_colour(tone)
    filled = int(clamp(used, 0, 100) / 10 + 0.5)
    marker = None if clock is None else clamp(int(clock / 10), 0, 9)

    cells = []
    for index in range(10):
        if index == marker:
            cells.append(rgb(235, 235, 245, "┃", colour))
        elif index < filled:
            cells.append(rgb(red, green, blue, "█", colour))
        else:
            cells.append(rgb(110, 110, 125, "░", colour))

    bar = "".join(cells)
    percentage = rgb(red, green, blue, f"{int(used + 0.5)}%".rjust(4), colour)
    reset_label = (
        rgb(110, 110, 125, f"  ↻  {reset_text}", colour)
        if reset is not None
        else " " * 14
    )

    return f"{bar} {percentage}{reset_label}"


def print_table(accounts, now, colour):
    """Print one row per account, marking an unreadable account unavailable on its own row.

    @param  {list}  accounts
        Account entries returned by account_data.
    @param  {int|float}  now
        Current time in Unix seconds.
    @param  {bool}  colour
        Whether to add terminal colour codes.
    """
    bold = "\033[1m" if colour else ""
    dim = "\033[2m" if colour else ""
    normal = "\033[0m" if colour else ""

    print(f"{bold}{'':12}  {'5h used':33}week used{normal}")

    for account in accounts:
        label = account["provider"]
        if account["account"] == "2":
            label += " (2)"

        if "error" in account:
            print(f"{label:12}  {dim}unavailable{normal}: {account['error']}")
            continue

        session = render_window(account["session"], now, colour)
        week = render_window(account["week"], now, colour)
        print(f"{label:12}  {session}    {week}")


def terminate(_signum, _frame):
    """Turn SIGTERM into SystemExit so a running Codex check still stops its app-server process.

    @param  {int}  _signum
        Signal number supplied to the handler and ignored.
    @param  {frame}  _frame
        Current stack frame supplied to the handler and ignored.
    """
    raise SystemExit(128 + signal.SIGTERM)


def main(argv=None):
    """Read every account and print the table, or JSON when --json is given."""
    parser = argparse.ArgumentParser(
        description="Show five-hour and weekly Claude and Codex quota use."
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON")
    args = parser.parse_args(argv)

    previous_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        accounts = [account_data(probe.account_usage(*account)) for account in ACCOUNTS]
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    if args.json:
        print(json.dumps({"ok": True, "data": {"accounts": accounts}}))
    else:
        colour = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        print_table(accounts, time.time(), colour)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
