"""Poll HCOM and redraw the terminal board."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from agent_board.board import (
    active_team_members,
    quiet_team_members,
    render_board,
    render_error,
)

# The terminal code that opens a separate screen for the board, so its frames
# stay out of the user's scrollback.
ENTER_ALT_SCREEN = "\x1b[?1049h"

# The terminal code that closes the board's screen and brings back what the
# terminal showed before the board started.
LEAVE_ALT_SCREEN = "\x1b[?1049l"

# The terminal code that hides the cursor while the board is running.
HIDE_CURSOR = "\x1b[?25l"

# The terminal code that shows the cursor again when the board closes.
SHOW_CURSOR = "\x1b[?25h"

# The terminal code that clears the pane so each refresh draws in place.
CLEAR_SCREEN = "\x1b[H\x1b[2J"

# How often the board reads team members' latest event times again.
ACTIVITY_POLL_SECONDS = 30

# How many non-blank lines at the bottom of a terminal are checked for a known
# failure. A live failure stays near the bottom of the pane, while an older one
# higher up may already be over.
RECENT_TERMINAL_LINES = 5


def main() -> int:
    """Redraw the board every two seconds until Ctrl+C.

    Returns 1 straight away when `hcom` is not on `PATH`. A failed or
    unreadable listing is shown on the board and tried again at the next
    refresh, so one bad call does not close the pane. Ctrl+C returns 0.

    In a terminal the board draws on its own screen. However it closes,
    including Ctrl+C, SIGTERM or an unexpected error, the terminal goes back
    to what it showed before, with the cursor visible.
    """
    if shutil.which("hcom") is None:
        print("agent-board: hcom is not installed or is not on PATH.", file=sys.stderr)
        return 1

    # Whether the board is drawing to a terminal rather than a pipe or file.
    terminal = sys.stdout.isatty()
    colour = terminal and "NO_COLOR" not in os.environ
    # When the board first saw each waiting agent with unread messages, so a
    # team can show as stuck once that wait passes the grace period.
    unread_since = {}
    # The time of each active team member's latest HCOM event, keyed by agent
    # name, so a team can show as stuck once all its members have gone quiet.
    last_activity = {}
    # When the board last read event times, so it reads them at most every
    # ACTIVITY_POLL_SECONDS.
    last_activity_checked_at = float("-inf")
    # The failure shown on each quiet member's terminal, keyed by agent name.
    # A member with no known failure maps to None, so its screen is read once.
    quiet_reasons = {}
    # Each team's current phase and known start time, so rows can show its
    # duration. Stuck phases use the last event or first unread time. This
    # history lasts only until the board restarts.
    phase_since = {}
    # The main checkout found for each agent directory, kept across refreshes
    # so Git runs once per directory.
    checkout_by_directory = {}

    def stop_on_sigterm(signum, frame):
        """Close the board when the process is told to stop.

        Exiting this way still restores the terminal, and the exit status is
        the shell's usual one for a process ended by a signal (143 for
        SIGTERM).
        """
        raise SystemExit(128 + signum)

    # The SIGTERM handler that was in place before the board started, put back
    # when the board closes.
    original_sigterm_handler = None
    try:
        if terminal:
            original_sigterm_handler = signal.signal(signal.SIGTERM, stop_on_sigterm)
            sys.stdout.write(ENTER_ALT_SCREEN)

        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()

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
                    _set_main_checkouts(agents, checkout_by_directory)

                    last_activity, last_activity_checked_at = _poll_activity(
                        agents, last_activity, last_activity_checked_at, now
                    )

                    quiet_members = quiet_team_members(agents, last_activity, now)
                    quiet_names = set(quiet_members.values())

                    for name in list(quiet_reasons):
                        if name not in quiet_names:
                            del quiet_reasons[name]

                    for name in quiet_names - quiet_reasons.keys():
                        quiet_reasons[name] = _read_terminal_reason(name)

                    lines = render_board(
                        agents,
                        width=width,
                        current_time=current_time,
                        colour=colour,
                        unread_since=unread_since,
                        last_activity=last_activity,
                        quiet_members=quiet_members,
                        quiet_reasons=quiet_reasons,
                        now=now,
                        phase_since=phase_since,
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
        try:
            if terminal:
                sys.stdout.write(SHOW_CURSOR + LEAVE_ALT_SCREEN)
            else:
                sys.stdout.write(SHOW_CURSOR + "\n")
            sys.stdout.flush()
        finally:
            if original_sigterm_handler is not None:
                signal.signal(signal.SIGTERM, original_sigterm_handler)


def _set_main_checkouts(agents: list[dict], cache: dict[str, str | None]) -> None:
    """Record each agent's main Git checkout, used to name worktree teams.

    Sets `main_checkout` on agents whose directory Git can trace back to a
    checkout, and leaves other agents unchanged. Results are cached by
    directory, including failures.
    """
    for agent in agents:
        directory = agent["directory"]

        if not directory:
            continue

        if directory not in cache:
            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        directory,
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-common-dir",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                cache[directory] = None
            else:
                common_directory = result.stdout.strip()
                cache[directory] = (
                    str(Path(common_directory).parent)
                    if result.returncode == 0 and Path(common_directory).name == ".git"
                    else None
                )

        if cache[directory]:
            agent["main_checkout"] = cache[directory]


def _poll_activity(
    agents: list[dict],
    last_activity: dict[str, float],
    checked_at: float,
    now: float,
) -> tuple[dict[str, float], float]:
    """Return active team members' event times and when they were last read.

    The times are read again only once ACTIVITY_POLL_SECONDS has passed since
    checked_at; until then the stored times and check time come back unchanged.
    """
    if now - checked_at < ACTIVITY_POLL_SECONDS:
        return last_activity, checked_at

    members = active_team_members(agents)

    if members:
        last_activity = _read_last_activity(members)
    else:
        last_activity = {}

    return last_activity, now


def _read_last_activity(agents: list[dict]) -> dict[str, float]:
    """Read each member's latest event as a monotonic activity time.

    A member whose event cannot be read has no time, so its team stays working.
    """
    last_activity = {}

    for agent in agents:
        try:
            result = subprocess.run(
                [
                    "hcom",
                    "events",
                    "--all",
                    "--agent",
                    agent["name"],
                    "--last",
                    "1",
                    "--full",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )

            if result.returncode or not result.stdout.strip():
                continue

            event = json.loads(result.stdout.splitlines()[-1])
            event_time = datetime.fromisoformat(event["ts"])

            if event_time.tzinfo is None:
                continue

            age = max(0, time.time() - event_time.timestamp())
            last_activity[agent["name"]] = time.monotonic() - age
        except (KeyError, TypeError, ValueError, subprocess.TimeoutExpired, OSError):
            continue

    return last_activity


def _read_terminal_reason(name: str) -> str | None:
    """Return a short reason when a quiet agent's screen shows a known failure.

    Return None when the screen cannot be read or shows no known failure.
    """
    try:
        result = subprocess.run(
            ["hcom", "term", name, "--json"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )

        if result.returncode:
            return None

        lines = json.loads(result.stdout)["lines"]

        if not isinstance(lines, list):
            return None

        recent_lines = [
            line.strip() for line in lines if isinstance(line, str) and line.strip()
        ][-RECENT_TERMINAL_LINES:]
        screen = " ".join(recent_lines).casefold()
    except (KeyError, TypeError, ValueError, subprocess.TimeoutExpired, OSError):
        return None

    # These phrases are copied from real Codex error lines, including the curly
    # apostrophe, so ordinary chat or code that mentions limits gives no reason.
    if "selected model is at capacity. please try a different model." in screen:
        return "model at capacity"

    if "you’ve hit your usage limit" in screen:
        return "usage limit reached"

    return None


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
