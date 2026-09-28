"""Poll HCOM and redraw the terminal board."""

import json
import os
import shutil
import subprocess
import sys
import time

from agent_board.board import render_board

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
    sys.stdout.write(HIDE_CURSOR)
    sys.stdout.flush()

    try:
        while True:
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
                    lines = [f"HCOM error: {message}"]
                else:
                    lines = render_board(json.loads(result.stdout), colour=colour)
            except KeyError as error:
                lines = [f"Unexpected hcom listing: missing {error}"]
            except TypeError as error:
                lines = [f"Unexpected hcom listing: {error}"]
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as error:
                lines = [f"HCOM error: {error}"]

            sys.stdout.write(CLEAR_SCREEN + "\n".join(lines) + "\n")
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()
