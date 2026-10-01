"""Poll HCOM and redraw the terminal board."""

import json
import os
import shutil
import subprocess
import sys
import time

from agent_board.board import render_board, render_error

# The terminal code that hides the cursor while the board is running.
HIDE_CURSOR = "\x1b[?25l"

# The terminal code that shows the cursor again when the board closes.
SHOW_CURSOR = "\x1b[?25h"

# The terminal code that clears the pane so each refresh draws in place.
CLEAR_SCREEN = "\x1b[H\x1b[2J"


def main() -> int:
    """Redraw the board every two seconds until Ctrl+C.

    Returns 1 straight away when `hcom` is not on `PATH`. A failed or
    unreadable listing is shown on the board and tried again at the next
    refresh, so one bad call does not close the pane. Ctrl+C returns 0.
    """
    if shutil.which("hcom") is None:
        print("agent-board: hcom is not installed or is not on PATH.", file=sys.stderr)
        return 1

    colour = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    # When the board first saw each waiting agent with unread messages, so a
    # team can show as stuck once that wait passes the grace period.
    unread_since = {}
    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    try:
        while True:
            width = shutil.get_terminal_size().columns
            current_time = time.strftime("%H:%M:%S")
            error_message = None

            try:
                result = subprocess.run(
                    ["hcom", "list", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

                if result.returncode:
                    message = (
                        result.stderr.strip() or f"hcom exited {result.returncode}"
                    )
                    error_message = f"HCOM error: {message}"
                else:
                    agents = json.loads(result.stdout)
                    now = time.monotonic()
                    _update_unread_since(agents, unread_since, now)
                    lines = render_board(
                        agents,
                        width=width,
                        current_time=current_time,
                        colour=colour,
                        unread_since=unread_since,
                        now=now,
                    )
            except KeyError as error:
                error_message = f"Unexpected hcom listing: missing {error}"
            except TypeError as error:
                error_message = f"Unexpected hcom listing: {error}"
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as error:
                error_message = f"HCOM error: {error}"

            if error_message is not None:
                lines = render_error(
                    error_message, width=width, current_time=current_time, colour=colour
                )

            sys.stdout.write(CLEAR_SCREEN + "\n".join(lines) + "\n")
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()


def _update_unread_since(
    agents: list[dict], unread_since: dict[str, float], now: float
) -> None:
    """Record when each waiting agent first had unread messages.

    Adds the current time for agents that have just started waiting with unread
    messages, keeps the earlier time for agents still in that state, and removes
    agents that have read their messages or stopped waiting. Changes
    `unread_since` in place.
    """
    unread_names = {
        agent["name"]
        for agent in agents
        if agent["status"] == "listening" and agent["unread_count"] > 0
    }

    for name in list(unread_since):
        if name not in unread_names:
            del unread_since[name]

    for name in unread_names:
        unread_since.setdefault(name, now)
