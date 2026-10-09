"""Check the board's recovery from HCOM process failures."""

import io
import json
import os
import subprocess

import pytest
from agent_board import cli
from agent_board.board import ANSI_STYLE_PATTERN


def test_missing_hcom_returns_one_without_hiding_cursor(monkeypatch) -> None:
    """A missing command exits before changing the terminal display."""
    output = io.StringIO()
    errors = io.StringIO()
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setattr(cli.sys, "stderr", errors)

    assert cli.main() == 1
    assert output.getvalue() == ""
    assert (
        errors.getvalue() == "agent-board: hcom is not installed or is not on PATH.\n"
    )


@pytest.mark.parametrize(
    ("terminal", "no_colour", "coloured"),
    [(False, False, False), (True, False, True), (True, True, False)],
)
def test_nonzero_hcom_exit_is_shown_on_the_board(
    monkeypatch, terminal: bool, no_colour: bool, coloured: bool
) -> None:
    """A failed HCOM command keeps its frame and follows the terminal colour setting."""
    output = io.StringIO()

    def failed_hcom(*args, **kwargs):
        """Return the exit status and error text from one failed listing."""
        return subprocess.CompletedProcess(
            args[0], 2, "", "listing failed\nretry later\n"
        )

    def stop_after_refresh(seconds):
        """End the board after one refresh."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", failed_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_refresh)
    monkeypatch.setattr(cli.time, "strftime", lambda format: "12:34:56")
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setattr(output, "isatty", lambda: terminal)
    monkeypatch.delenv("NO_COLOR", raising=False)

    if no_colour:
        monkeypatch.setenv("NO_COLOR", "1")

    assert cli.main() == 0
    board_output = (
        output.getvalue().split(cli.CLEAR_SCREEN)[1].split(cli.SHOW_CURSOR)[0]
    )
    plain_output = ANSI_STYLE_PATTERN.sub("", board_output)

    assert "╭─ ✻ agent board " in plain_output
    assert "12:34:56 ─╮" in plain_output
    assert "│" + " " * 78 + "│\n│  HCOM error: listing failed" in plain_output
    assert "│  retry later" in plain_output
    assert "│" + " " * 78 + "│\n╰" in plain_output
    assert "╰" + "─" * 78 + "╯" in plain_output

    if coloured:
        assert "\x1b[38;5;214m╭" in board_output
    else:
        assert "\x1b[" not in board_output

    assert output.getvalue().endswith(cli.SHOW_CURSOR + "\n")


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ([{"status": "listening", "tag": "Team-scout"}], "missing 'unread_count'"),
        (None, "'NoneType' object is not iterable"),
    ],
)
def test_unreadable_listing_is_reported_separately(
    monkeypatch, listing, expected
) -> None:
    """A listing with an unexpected shape does not look like an HCOM failure."""
    output = io.StringIO()

    def unreadable_hcom(*args, **kwargs):
        """Return one listing whose fields the board cannot read."""
        return subprocess.CompletedProcess(args[0], 0, json.dumps(listing), "")

    def stop_after_refresh(seconds):
        """End the board after one refresh."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", unreadable_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_refresh)
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    assert f"│  Unexpected hcom listing: {expected}" in output.getvalue()
    assert "╰" + "─" * 78 + "╯" in output.getvalue()


def test_hcom_disappearing_shows_error_and_restores_cursor(monkeypatch) -> None:
    """An HCOM launch failure stays visible until the next refresh."""
    output = io.StringIO()

    def missing_hcom(*args, **kwargs):
        """Simulate HCOM disappearing after the initial PATH check."""
        raise OSError("hcom disappeared")

    def stop_after_refresh(seconds):
        """End the board after its first bounded refresh."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", missing_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_refresh)
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    result = cli.main()

    assert result == 0
    assert "│  HCOM error: hcom disappeared" in output.getvalue()
    assert output.getvalue().endswith(cli.SHOW_CURSOR + "\n")


def test_each_refresh_uses_current_width_and_time(monkeypatch) -> None:
    """A resized pane and changed clock appear on the next refresh."""
    output = io.StringIO()
    sizes = iter([os.terminal_size((60, 24)), os.terminal_size((70, 24))])
    times = iter(["12:34:56", "12:34:58"])
    refreshes = 0

    def listed_hcom(*args, **kwargs):
        """Return an empty listing for both refreshes."""
        return subprocess.CompletedProcess(args[0], 0, "[]", "")

    def stop_after_two_refreshes(seconds):
        """Allow one more refresh before closing the board."""
        nonlocal refreshes
        refreshes += 1

        if refreshes == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda: next(sizes))
    monkeypatch.setattr(cli.time, "strftime", lambda format: next(times))
    monkeypatch.setattr(cli.subprocess, "run", listed_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_two_refreshes)
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    assert output.getvalue().count(cli.CLEAR_SCREEN) == 2
    assert "╭─ ✻ agent board " in output.getvalue()
    assert "12:34:56 ─╮" in output.getvalue()
    assert "12:34:58 ─╮" in output.getvalue()

    frames = output.getvalue().split(cli.CLEAR_SCREEN)[1:]
    assert len(frames[0].splitlines()[1]) == 60
    assert len(frames[1].splitlines()[1]) == 70


def test_refreshes_keep_the_phase_timer_until_the_displayed_status_changes(
    monkeypatch,
) -> None:
    """The running board keeps one phase history across HCOM listings."""
    output = io.StringIO()
    refreshes = 0
    times = iter([100, 175, 185])
    members = [
        {
            "name": "Agent-Tools-orchestrator",
            "tag": "Agent-Tools-orchestrator",
            "directory": "/work/Agent Tools",
            "status": "listening",
            "unread_count": 0,
        },
        {
            "name": "Agent-Tools-scout",
            "tag": "Agent-Tools-scout",
            "directory": "/work/Agent Tools",
            "status": "listening",
            "unread_count": 0,
        },
    ]

    def listed_hcom(command, **kwargs):
        """Return waiting, checking, then waiting team listings."""
        listing = [dict(member) for member in members]

        if refreshes == 1:
            listing[1]["status"] = "active"

        return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")

    def stop_after_three_refreshes(seconds):
        """Close the board after the third bounded refresh."""
        nonlocal refreshes
        refreshes += 1

        if refreshes == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.subprocess, "run", listed_hcom)
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli.time, "sleep", stop_after_three_refreshes)
    monkeypatch.setattr(
        cli, "_poll_activity", lambda agents, previous, checked, now: ({}, now)
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    frames = output.getvalue().split(cli.CLEAR_SCREEN)[1:]
    plain_frames = [ANSI_STYLE_PATTERN.sub("", frame) for frame in frames]

    assert "● needs you · 0s+" in plain_frames[0]
    assert "◌ checking · 0s" in plain_frames[1]
    assert "● needs you · 0s" in plain_frames[2]
    assert "● needs you · 0s+" not in plain_frames[2]


def test_latest_event_time_becomes_a_monotonic_time(monkeypatch) -> None:
    """An event timestamp becomes a monotonic time for the board's quiet rule."""
    commands = []

    def event_hcom(command, **kwargs):
        """Return an event from five minutes before the current wall time."""
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"ts": "1970-01-01T00:01:00+00:00"}), ""
        )

    monkeypatch.setattr(cli.subprocess, "run", event_hcom)
    monkeypatch.setattr(cli.time, "time", lambda: 360)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 1000)

    activity = cli._read_last_activity([{"name": "team-scout"}])

    assert activity == {"team-scout": 700}
    assert commands == [
        ["hcom", "events", "--all", "--agent", "team-scout", "--last", "1", "--full"]
    ]


@pytest.mark.parametrize(
    ("output", "returncode", "times_out"),
    [
        ('{"ts": "1970-01-01T00:01:00+00:00"}', 1, False),
        ('{"ts": "1970-01-01T00:01:00"}', 0, False),
        ("not JSON", 0, False),
        ("", 0, True),
    ],
)
def test_unreadable_event_has_no_activity_time(
    monkeypatch, output: str, returncode: int, times_out: bool
) -> None:
    """A failed or unreadable event cannot make a team appear quiet."""

    def event_hcom(command, **kwargs):
        """Return one unreadable event or a bounded timeout."""
        if times_out:
            raise subprocess.TimeoutExpired(command, 2)

        return subprocess.CompletedProcess(command, returncode, output, "")

    monkeypatch.setattr(cli.subprocess, "run", event_hcom)

    assert cli._read_last_activity([{"name": "team-scout"}]) == {}


@pytest.mark.parametrize(
    ("screen", "reason"),
    [
        (
            "Selected model is at capacity. Please try a different model.",
            "model at capacity",
        ),
        ("You’ve hit your usage limit. Upgrade to Pro.", "usage limit reached"),
        ("A note about the rate limit.", None),
        ("The agent is waiting for input.", None),
    ],
)
def test_terminal_reason_matches_known_failures(monkeypatch, screen, reason) -> None:
    """Only known failure text adds a reason to the stuck row."""

    def terminal_hcom(command, **kwargs):
        """Return a readable terminal with the selected text."""
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"lines": [screen]}), ""
        )

    monkeypatch.setattr(cli.subprocess, "run", terminal_hcom)

    assert cli._read_terminal_reason("team-scout") == reason


def test_terminal_reason_ignores_failures_higher_up_the_screen(monkeypatch) -> None:
    """An old failure above the recent terminal lines gives no reason."""
    lines = ["Selected model is at capacity. Please try a different model."] + [
        f"Normal output {index}" for index in range(5)
    ]

    def terminal_hcom(command, **kwargs):
        """Return an old failure followed by five unrelated lines."""
        return subprocess.CompletedProcess(command, 0, json.dumps({"lines": lines}), "")

    monkeypatch.setattr(cli.subprocess, "run", terminal_hcom)

    assert cli._read_terminal_reason("team-scout") is None


def test_quiet_team_polls_activity_and_terminal_once(monkeypatch) -> None:
    """Frequent redraws reuse recent event times and the first terminal reading."""
    output = io.StringIO()
    members = [
        {
            "directory": "/work/Agent Tools",
            "name": "Team-orchestrator",
            "status": "listening",
            "status_age_seconds": 0,
            "tag": "Team-orchestrator",
            "unread_count": 0,
        },
        {
            "directory": "/work/Agent Tools",
            "name": "Team-scout",
            "status": "active",
            "status_age_seconds": 600,
            "tag": "Team-scout",
            "unread_count": 0,
        },
    ]
    event_polls = []
    terminal_polls = []
    refreshes = 0

    def listed_hcom(command, **kwargs):
        """Return the same active team for each refresh."""
        return subprocess.CompletedProcess(command, 0, json.dumps(members), "")

    def read_events(agents):
        """Record which agents have their events polled."""
        event_polls.append([agent["name"] for agent in agents])
        return {agent["name"]: 0 for agent in agents}

    def read_terminal(name):
        """Record the one terminal read for the quiet member."""
        terminal_polls.append(name)
        return "model at capacity"

    def stop_after_four_refreshes(seconds):
        """End the board after its fourth bounded refresh."""
        nonlocal refreshes
        refreshes += 1

        if refreshes == 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_read_last_activity", read_events)
    monkeypatch.setattr(cli, "_read_terminal_reason", read_terminal)
    monkeypatch.setattr(cli.subprocess, "run", listed_hcom)
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.time, "monotonic", iter([1000, 1002, 1004, 1006]).__next__)
    monkeypatch.setattr(cli.time, "sleep", stop_after_four_refreshes)
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    assert event_polls == [["Team-orchestrator", "Team-scout"]]
    assert terminal_polls == ["Team-scout"]
    assert "stuck · model at capacity" in output.getvalue()


def test_activity_is_read_again_after_thirty_seconds(monkeypatch) -> None:
    """The board reads event times again once the polling interval has passed."""
    member = {
        "directory": "/work/Agent Tools",
        "name": "Team-scout",
        "status": "active",
        "status_age_seconds": 600,
        "tag": "Team-scout",
        "unread_count": 0,
    }
    readings = []

    def read_events(agents):
        """Record each activity read for the active member."""
        readings.append([agent["name"] for agent in agents])
        return {member["name"]: 1000}

    monkeypatch.setattr(cli, "_read_last_activity", read_events)

    last_activity = {}
    checked_at = float("-inf")

    for now in (1000, 1029, 1030):
        last_activity, checked_at = cli._poll_activity(
            [member], last_activity, checked_at, now
        )

    assert readings == [["Team-scout"], ["Team-scout"]]
    assert checked_at == 1030


def test_activity_times_clear_when_no_team_is_active() -> None:
    """The board drops old activity times after every active team stops."""
    member = {"name": "Team-scout", "status": "listening", "tag": "Team-scout"}

    last_activity, checked_at = cli._poll_activity([member], {member["name"]: 0}, 0, 30)

    assert last_activity == {}
    assert checked_at == 30


def test_new_event_clears_quiet_reason_and_returns_team_to_working(monkeypatch) -> None:
    """Fresh activity removes the terminal reason and restores the working row."""
    output = io.StringIO()
    member = {
        "directory": "/work/Agent Tools",
        "name": "Team-scout",
        "status": "active",
        "status_age_seconds": 600,
        "tag": "Team-scout",
        "unread_count": 0,
    }
    event_times = iter([0, 1030])
    terminal_polls = []
    refreshes = 0

    def listed_hcom(command, **kwargs):
        """Keep the same member active across three refreshes."""
        return subprocess.CompletedProcess(command, 0, json.dumps([member]), "")

    def read_events(agents):
        """Return a new event on the second activity read."""
        return {member["name"]: next(event_times)}

    def read_terminal(name):
        """Record the first quiet transition and its failure reason."""
        terminal_polls.append(name)
        return "model at capacity"

    def stop_after_three_refreshes(seconds):
        """End the board after the fresh activity has been drawn."""
        nonlocal refreshes
        refreshes += 1

        if refreshes == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_read_last_activity", read_events)
    monkeypatch.setattr(cli, "_read_terminal_reason", read_terminal)
    monkeypatch.setattr(cli.subprocess, "run", listed_hcom)
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.time, "monotonic", iter([1000, 1002, 1030]).__next__)
    monkeypatch.setattr(cli.time, "sleep", stop_after_three_refreshes)
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    frames = output.getvalue().split(cli.CLEAR_SCREEN)[1:]
    assert "stuck · model at capacity" in frames[0]
    assert "stuck · model at capacity" in frames[1]
    assert "stuck · model at capacity" not in frames[2]
    assert "WORKING" in frames[2]
    assert "checking" in frames[2]
    assert terminal_polls == ["Team-scout"]
