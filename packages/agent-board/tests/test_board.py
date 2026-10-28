"""Check board decisions against representative HCOM listings."""

import re

import pytest
from agent_board.board import (
    BoardState,
    lists_team,
    place_quota_panel,
    quiet_team_members,
    render_board,
    render_error,
)
from agent_board.cli import _update_unread_since
from agent_board.frame import ANSI_STYLE_PATTERN, frame_lines
from agent_board.progress_lookup import TaskReading


def agent(
    prefix: str,
    role: str | None,
    *,
    directory: str = "/work/Agent Tools",
    status: str = "listening",
    age: int = 60,
    unread: int = 0,
) -> dict:
    """Build a listing row for a team prefix, with an optional role suffix."""
    return {
        "directory": directory,
        "name": f"{prefix}-{role}" if role else prefix,
        "status": status,
        "status_age_seconds": age,
        "tag": f"{prefix}-{role}" if role else prefix,
        "unread_count": unread,
    }


def board_content(
    agents: list[dict],
    *,
    colour: bool = False,
    unread_since: dict[str, float] | None = None,
    last_activity: dict[str, float] | None = None,
    quiet_reasons: dict[str, str | None] | None = None,
    now: float = 0,
    phase_since: dict[str, tuple[str, float, bool]] | None = None,
) -> list[str]:
    """Return the rows inside the frame, with plain rows' padding cut to two spaces.

    This lets tests compare a name and its status without counting columns.
    Coloured rows come back as drawn, so tests can check exactly where colour
    codes start and stop.
    """
    quiet_members = (
        quiet_team_members(agents, last_activity, now)
        if last_activity is not None
        else {}
    )
    framed = render_board(
        agents,
        BoardState(
            unread_since=unread_since or {},
            last_activity=last_activity or {},
            quiet_members=quiet_members,
            quiet_reasons=quiet_reasons or {},
            now=now,
            phase_since=phase_since,
        ),
        width=80,
        current_time="12:34:56",
        colour=colour,
    )
    rows = []

    for row in framed[3:-2]:
        content = re.sub(r"^(?:\x1b\[[0-9;]*m)?│(?:\x1b\[[0-9;]*m)?  ", "", row)
        content = re.sub(r"  (?:\x1b\[[0-9;]*m)?│(?:\x1b\[[0-9;]*m)?$", "", content)
        rows.append(
            re.sub(r" {2,}", "  ", content).rstrip() if not colour else content.rstrip()
        )

    return rows


@pytest.mark.parametrize("frame_style", [None, "\x1b[33m"])
def test_frame_has_unbroken_top_edge_without_right_text(
    frame_style: str | None,
) -> None:
    """An empty status leaves no spaces before the frame's top-right corner."""
    lines = frame_lines([], 30, "", None, frame_style=frame_style, title="Quota")
    top = ANSI_STYLE_PATTERN.sub("", lines[1])

    assert top == "╭─ Quota " + "─" * 20 + "╮"


def test_listed_team_matches_the_board_grouping() -> None:
    """Blocked and listening members count; inactive members do not."""
    assert lists_team([]) is False
    assert lists_team([agent("Team", None, status="inactive")]) is False
    assert lists_team([agent("Team", None, status="blocked")]) is True
    assert lists_team([agent("Team", None, status="listening")]) is True


def test_phase_timer_starts_as_an_estimate_and_resets_when_the_status_changes() -> None:
    """Only a phase first seen by the board carries an estimated start."""
    phase_since = {}
    waiting = [agent("Agent-Tools", "orchestrator"), agent("Agent-Tools", "scout")]
    checking = [
        agent("Agent-Tools", "orchestrator"),
        agent("Agent-Tools", "scout", status="active"),
    ]

    assert board_content(waiting, now=100, phase_since=phase_since)[1] == (
        "● Agent-Tools  needs you · 0s+"
    )
    assert board_content(waiting, now=172, phase_since=phase_since)[1] == (
        "● Agent-Tools  needs you · 1m+"
    )
    assert board_content(checking, now=180, phase_since=phase_since)[1] == (
        "◌ Agent-Tools  checking · 0s"
    )
    assert board_content(checking, now=245, phase_since=phase_since)[1] == (
        "◌ Agent-Tools  checking · 1m"
    )
    assert board_content(waiting, now=250, phase_since=phase_since)[1] == (
        "● Agent-Tools  needs you · 0s"
    )


def test_disappearing_team_gets_a_new_estimated_baseline() -> None:
    """A returning team cannot reuse its earlier observed phase time."""
    phase_since = {}
    members = [agent("Agent-Tools", "scout", status="active")]

    assert board_content(members, now=100, phase_since=phase_since)[1] == (
        "◌ Agent-Tools (partial team)  checking · 0s+"
    )
    assert board_content([], now=120, phase_since=phase_since) == ["No active teams"]
    assert board_content(members, now=200, phase_since=phase_since)[1] == (
        "◌ Agent-Tools (partial team)  checking · 0s+"
    )


def test_blocked_row_uses_hcom_age_instead_of_a_phase_timer() -> None:
    """The blocked row keeps HCOM's longest wait without an estimate suffix."""
    members = [agent("Agent-Tools", "scout", status="blocked", age=240)]

    assert board_content(members, now=100, phase_since={})[1] == (
        "✕ Agent-Tools  blocked · 4m"
    )


def test_quiet_team_stuck_timer_starts_at_its_last_event() -> None:
    """The quiet timer includes the silence before the board called it stuck."""
    phase_since = {}
    members = [
        agent("Agent-Tools", "orchestrator"),
        agent("Agent-Tools", "scout", status="active"),
    ]
    last_activity = {member["name"]: 0 for member in members}

    board_content(
        members, last_activity=last_activity, now=299, phase_since=phase_since
    )

    assert (
        board_content(
            members, last_activity=last_activity, now=300, phase_since=phase_since
        )[1]
        == "● Agent-Tools  stuck · 5m"
    )


def test_unread_team_stuck_timer_starts_at_its_first_old_unread_message() -> None:
    """The unread timer includes the wait before it crossed the stuck limit."""
    phase_since = {}
    members = [
        agent("Agent-Tools", "orchestrator", unread=1),
        agent("Agent-Tools", "scout", unread=1),
    ]
    unread_since = {members[0]["name"]: 0, members[1]["name"]: 30}

    board_content(members, unread_since=unread_since, now=60, phase_since=phase_since)

    assert (
        board_content(
            members, unread_since=unread_since, now=61, phase_since=phase_since
        )[1]
        == "● Agent-Tools  stuck · 1m"
    )


def test_changing_a_stuck_reason_does_not_reset_the_timer() -> None:
    """A new terminal reason leaves the team's stuck phase running."""
    phase_since = {}
    members = [
        agent("Agent-Tools", "orchestrator"),
        agent("Agent-Tools", "scout", status="active"),
    ]
    last_activity = {member["name"]: 0 for member in members}
    worker = members[1]["name"]

    assert (
        board_content(
            members,
            last_activity=last_activity,
            quiet_reasons={worker: "model at capacity"},
            now=300,
            phase_since=phase_since,
        )[1]
        == "● Agent-Tools  stuck · model at capacity · 5m+"
    )
    assert (
        board_content(
            members,
            last_activity=last_activity,
            quiet_reasons={worker: "rate limited"},
            now=362,
            phase_since=phase_since,
        )[1]
        == "● Agent-Tools  stuck · rate limited · 6m+"
    )


def test_frame_shows_mixed_groups_and_counts_at_full_width() -> None:
    """A mixed board puts headings and totals inside a full-width frame."""
    agents = [
        agent("Agent-Tools-idle", "orchestrator"),
        agent("Agent-Tools-idle", "scout"),
        agent("Agent-Tools-working", "orchestrator", status="active"),
        agent("Agent-Tools-working", "scout"),
    ]

    lines = render_board(agents, width=60, current_time="12:34:56")

    assert lines[0] == ""
    assert lines[1].startswith("╭─ ✻ agent board ")
    assert lines[1].endswith(" 12:34:56 ─╮")
    assert lines[2] == "│" + " " * 58 + "│"
    assert lines[3] == "│  WAITING ON YOU" + " " * 40 + "  │"
    assert lines[4].startswith("│  ● Agent-Tools")
    assert lines[4].endswith("needs you  │")
    assert lines[5] == lines[2]
    assert lines[6] == "│  WORKING" + " " * 47 + "  │"
    assert lines[7].startswith("│  ○ Agent-Tools")
    assert lines[7].endswith("coordinating  │")
    assert lines[-2] == lines[2]
    assert lines[-1].startswith("╰─ 1 waiting on you · 1 working ")
    assert lines[-1].endswith("─╯")
    assert all(len(line) == 60 for line in lines[1:])


def test_task_and_chunk_lines_follow_teams_in_both_groups() -> None:
    """Each known task sits directly under its waiting or working team."""
    agents = [
        agent("Waiting", "orchestrator"),
        agent("Waiting", "scout"),
        agent("Working", "scout", status="active"),
    ]
    readings = {
        "Waiting": TaskReading("Plan the release", "Review the draft"),
        "Working": TaskReading("Build the board", "Show team tasks"),
    }

    lines = render_board(
        agents, BoardState(task_readings=readings), width=80, current_time="12:34:56"
    )

    assert "Waiting" in lines[4]
    assert "Task: Plan the release · Chunk: Review the draft" in lines[5]
    assert "Working" in lines[8]
    assert "Task: Build the board · Chunk: Show team tasks" in lines[9]


def test_task_lines_are_dimmed_in_both_groups() -> None:
    """Task details use the muted style under waiting and working teams."""
    agents = [
        agent("Waiting", "scout", status="blocked"),
        agent("Working", "scout", status="active"),
    ]
    readings = {
        "Waiting": TaskReading("Plan the release", None),
        "Working": TaskReading("Build the board", "Show team tasks"),
    }

    lines = render_board(
        agents,
        BoardState(task_readings=readings),
        width=80,
        current_time="12:34:56",
        colour=True,
    )
    task_lines = [line for line in lines if "Task:" in line]

    assert len(task_lines) == 2
    assert all("\x1b[2m  Task:" in line for line in task_lines)
    assert all("\x1b[0m" in line for line in task_lines)
    assert all(len(ANSI_STYLE_PATTERN.sub("", line)) == 80 for line in task_lines)


def test_task_line_shows_task_alone_and_skips_teams_without_a_task() -> None:
    """A task without an active chunk has one label, and unknown teams have none."""
    agents = [agent("Known", "scout", status="active"), agent("Unknown", "scout")]

    lines = render_board(
        agents,
        BoardState(task_readings={"Known": TaskReading("Build the board", None)}),
        width=80,
        current_time="12:34:56",
    )

    assert sum("Task:" in line for line in lines) == 1
    assert "Task: Build the board" in "\n".join(lines)
    assert "Chunk:" not in "\n".join(lines)


def test_task_line_is_shortened_to_the_80_column_frame() -> None:
    """A long task stays within the capped frame on a wide terminal."""
    agents = [agent("Working", "scout", status="active")]

    lines = render_board(
        agents,
        BoardState(task_readings={"Working": TaskReading("A" * 100, "B" * 100)}),
        width=200,
        current_time="12:34:56",
    )

    assert lines[5].endswith("…  │")
    assert len(lines[5]) == 80


def test_plain_board_below_30_columns_omits_task_lines() -> None:
    """A compact board shows only team rows even when task readings exist."""
    agents = [agent("Working", "scout", status="active")]

    lines = render_board(
        agents,
        BoardState(
            task_readings={"Working": TaskReading("Build the board", "Show team tasks")}
        ),
        width=29,
        current_time="12:34:56",
    )

    assert len(lines) == 2
    assert "Task:" not in "\n".join(lines)


@pytest.mark.parametrize(
    ("agents", "border_style"),
    [
        ([agent("Agent-Tools", "scout", status="blocked")], "\x1b[31m"),
        (
            [agent("Agent-Tools", "orchestrator"), agent("Agent-Tools", "scout")],
            "\x1b[35m",
        ),
        ([agent("Agent-Tools", "scout", status="active")], "\x1b[38;5;214m"),
        ([], "\x1b[38;5;214m"),
    ],
)
def test_frame_colour_follows_the_board_state(
    agents: list[dict], border_style: str
) -> None:
    """The border and counts show waiting teams, or amber when none is waiting."""
    lines = render_board(agents, width=60, current_time="12:34:56", colour=True)

    assert lines[1].startswith(f"{border_style}╭─ ")
    assert "\x1b[38;5;214m✻ agent board\x1b[0m" in lines[1]
    assert "\x1b[38;5;214m12:34:56\x1b[0m" in lines[1]
    assert lines[2].startswith(f"{border_style}│\x1b[0m")
    assert lines[-1].startswith(f"{border_style}╰─ ")
    assert lines[-1].endswith("─╯\x1b[0m")
    assert all(len(ANSI_STYLE_PATTERN.sub("", line)) == 60 for line in lines[1:])


def test_colourless_frame_contains_no_terminal_styles() -> None:
    """A plain output stream keeps the same frame without ANSI codes."""
    agents = [agent("Agent-Tools", "scout", status="blocked")]

    lines = render_board(agents, width=60, current_time="12:34:56", colour=False)

    assert all("\x1b[" not in line for line in lines)


def test_wide_terminal_keeps_an_80_column_frame_left_aligned() -> None:
    """A wide terminal leaves unused columns to the right of the frame."""
    agents = [agent("Agent-Tools", "scout", status="active")]

    lines = render_board(agents, width=200, current_time="12:34:56")

    assert all(len(line) == 80 for line in lines[1:])
    assert lines[1].endswith(" 12:34:56 ─╮")
    assert lines[-1].startswith("╰─ 0 waiting on you · 1 working ")


def test_80_column_terminal_keeps_its_existing_frame() -> None:
    """The cap does not alter a frame at exactly 80 columns."""
    agents = [agent("Agent-Tools", "scout", status="active")]

    at_cap = render_board(agents, width=80, current_time="12:34:56")
    above_cap = render_board(agents, width=200, current_time="12:34:56")

    assert at_cap == above_cap
    assert all(len(line) == 80 for line in at_cap[1:])


def test_wide_terminal_shortens_a_long_row_at_the_frame_cap() -> None:
    """A long team name shortens while the status stays at the capped edge."""
    agents = [agent("Agent-Tools-" + "long-" * 20, "scout", status="active")]

    lines = render_board(agents, width=200, current_time="12:34:56")

    assert "…" in lines[4]
    assert lines[4].endswith("checking  │")
    assert all(len(line) == 80 for line in lines[1:])


@pytest.mark.parametrize("colour", [False, True])
def test_long_names_shorten_and_keep_full_status_in_both_groups(colour: bool) -> None:
    """Waiting and working rows keep the full status when names are long."""
    agents = [
        agent("Agent-Tools-" + "blocked-" * 8, "scout", status="blocked"),
        agent("Agent-Tools-" + "working-" * 8, "scout", status="active"),
    ]

    lines = render_board(agents, width=42, current_time="12:34:56", colour=colour)
    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert plain_lines[4].startswith("│  ✕ Agent-Tools")
    assert "…  blocked · 1m  │" in plain_lines[4]
    assert plain_lines[7].startswith("│  ◌ Agent-Tools")
    assert "…" in plain_lines[7]
    assert plain_lines[7].endswith("checking  │")
    assert all(len(line) == 42 for line in plain_lines[1:])


@pytest.mark.parametrize("colour", [False, True])
def test_long_status_keeps_start_of_team_name_at_40_columns(colour: bool) -> None:
    """A long status loses its end while the team name keeps three letters."""
    worker = agent("Agent-Tools", "scout", status="active")
    lines = render_board(
        [worker],
        BoardState(
            last_activity={worker["name"]: 0},
            quiet_members=quiet_team_members([worker], {worker["name"]: 0}, 300),
            quiet_reasons={worker["name"]: "model at capacity"},
            now=300,
            phase_since={},
        ),
        width=40,
        current_time="12:34:56",
        colour=colour,
    )
    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert plain_lines[4].startswith("│  ● Age…")
    assert "stuck · model at capacity" in plain_lines[4]
    assert len(plain_lines[4]) == 40


@pytest.mark.parametrize("colour", [False, True])
def test_short_status_keeps_full_name_beside_long_status(colour: bool) -> None:
    """Each team name uses the space left by its own right-aligned status."""
    worker = agent("Agent-Tools-long-name", "scout", status="active")
    waiting = [
        agent("Agent-Tools-agent-board", "orchestrator"),
        agent("Agent-Tools-agent-board", "scout"),
    ]
    lines = render_board(
        [worker, *waiting],
        BoardState(
            last_activity={worker["name"]: 0},
            quiet_members=quiet_team_members([worker], {worker["name"]: 0}, 300),
            quiet_reasons={worker["name"]: "model at capacity"},
            now=300,
            phase_since={},
        ),
        width=50,
        current_time="12:34:56",
        colour=colour,
    )
    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert any(
        "Agent-Tools · agent-board" in line and line.endswith("needs you · 0s+  │")
        for line in plain_lines
    )
    assert any(
        line.endswith("stuck · model at capacity · 5m+  │") for line in plain_lines
    )


@pytest.mark.parametrize("colour", [False, True])
def test_frame_pads_coloured_and_plain_rows_to_the_same_width(colour: bool) -> None:
    """Both groups end their statuses at the same edge in either colour mode."""
    agents = [
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-working", "scout", status="active"),
    ]

    lines = render_board(agents, width=70, current_time="01:02:03", colour=colour)

    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert plain_lines[4].startswith("│  ✕ Agent-Tools · blocked")
    assert plain_lines[4].endswith("blocked · 2m  │")
    assert plain_lines[7].startswith("│  ◌ Agent-Tools · working (partial team)")
    assert plain_lines[7].endswith("checking  │")
    assert all(len(ANSI_STYLE_PATTERN.sub("", line)) == 70 for line in lines[1:])


@pytest.mark.parametrize("colour", [False, True])
def test_empty_board_keeps_message_inside_frame(colour: bool) -> None:
    """An empty board pads its message and shows zero counts in either colour mode."""
    lines = render_board([], width=50, current_time="09:08:07", colour=colour)

    assert len(lines) == 6
    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert plain_lines[2] == "│" + " " * 48 + "│"
    assert plain_lines[3].startswith("│  ")
    assert "No active teams" in plain_lines[3]
    assert lines[-2] == lines[2]
    assert plain_lines[-1].startswith("╰─ 0 waiting on you · 0 working ")
    assert all(len(line) == 50 for line in plain_lines[1:])


def test_error_frame_uses_plain_bottom_edge() -> None:
    """A failed listing shows its message without making a team-count claim."""
    lines = render_error(
        "HCOM error: listing failed", width=50, current_time="09:08:07"
    )

    assert lines[1].startswith("╭─ ✻ agent board ")
    assert lines[2] == "│" + " " * 48 + "│"
    assert lines[3] == "│  HCOM error: listing failed" + " " * 18 + "  │"
    assert lines[-2] == lines[2]
    assert lines[-1] == "╰" + "─" * 48 + "╯"
    assert all(len(line) == 50 for line in lines[1:])


@pytest.mark.parametrize("colour", [False, True])
def test_error_frame_uses_amber_only_with_colour(colour: bool) -> None:
    """An error keeps its frame and uses amber only when colour is enabled."""
    lines = render_error(
        "HCOM error: listing failed",
        width=50,
        current_time="09:08:07",
        colour=colour,
    )

    if colour:
        assert lines[1].startswith("\x1b[38;5;214m╭─ ")
        assert "\x1b[38;5;214m✻ agent board\x1b[0m" in lines[1]
        assert "\x1b[38;5;214m09:08:07\x1b[0m" in lines[1]
        assert lines[2].startswith("\x1b[38;5;214m│\x1b[0m")
        assert lines[-1].startswith("\x1b[38;5;214m╰")
    else:
        assert all("\x1b[" not in line for line in lines)

    assert all(len(ANSI_STYLE_PATTERN.sub("", line)) == 50 for line in lines[1:])


def test_multiline_error_keeps_every_line_inside_the_frame() -> None:
    """Each line of an HCOM error has its own left and right border."""
    lines = render_error(
        "HCOM error: listing failed\nretry later",
        width=50,
        current_time="09:08:07",
    )

    assert lines[2] == "│" + " " * 48 + "│"
    assert lines[3].startswith("│  HCOM error: listing failed")
    assert lines[4].startswith("│  retry later")
    assert all(line.endswith("  │") for line in lines[3:5])
    assert lines[-2] == lines[2]
    assert lines[-1] == "╰" + "─" * 48 + "╯"
    assert all(len(line) == 50 for line in lines[1:])


@pytest.mark.parametrize("colour", [False, True])
def test_tight_frame_shortens_rows_and_bottom_text(colour: bool) -> None:
    """The frame stays aligned when team rows and the counts exceed its width."""
    agents = [
        agent("Agent-Tools-long-blocked-team", "scout", status="blocked"),
        agent("Agent-Tools-long-working-team", "scout", status="active"),
    ]

    lines = render_board(agents, width=30, current_time="12:34:56", colour=colour)
    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert plain_lines[1].startswith("╭─ ✻ agent board ")
    assert plain_lines[1].endswith(" 12:34:56 ─╮")
    assert plain_lines[4].startswith("│  ✕ Agent-T…")
    assert plain_lines[4].endswith("blocked · 1m  │")
    assert plain_lines[7].endswith("checking  │")
    assert plain_lines[-1] == "╰─ 1 waiting on you · 1 wo… ─╯"
    assert all(len(line) == 30 for line in plain_lines[1:])

    if colour:
        assert "…\x1b[0m" in lines[4]
    else:
        assert all("\x1b[" not in line for line in lines)


def test_long_error_line_stays_inside_a_tight_frame() -> None:
    """A long hcom error cannot push the right edge past the terminal width."""
    lines = render_error(
        "HCOM error: a very long failure message",
        width=30,
        current_time="12:34:56",
    )

    assert lines[3] == "│  HCOM error: a very long…  │"
    assert all(len(line) == 30 for line in lines[1:])


@pytest.mark.parametrize("colour", [False, True])
def test_narrow_board_uses_plain_rows_with_a_gap_between_groups(colour: bool) -> None:
    """A narrow board keeps both groups without headings, a clock, or counts."""
    agents = [
        agent("Agent-Tools-blocked", "scout", status="blocked"),
        agent("Agent-Tools-working", "scout", status="active"),
    ]

    lines = render_board(agents, width=29, current_time="12:34:56", colour=colour)
    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert plain_lines == [
        "",
        "✕ Agent-Tools …  blocked · 1m",
        "",
        "◌ Agent-Tools · wo…  checking",
    ]
    assert all(len(line) <= 29 for line in plain_lines)

    if colour:
        assert lines[1].endswith("blocked · 1m\x1b[0m")
        assert lines[3].endswith("checking\x1b[0m")
    else:
        assert all("\x1b[" not in line for line in lines)


@pytest.mark.parametrize("colour", [False, True])
def test_narrow_empty_board_keeps_plain_empty_message(colour: bool) -> None:
    """The empty state remains readable without a frame or counts."""
    lines = render_board([], width=10, current_time="12:34:56", colour=colour)

    assert ANSI_STYLE_PATTERN.sub("", lines[1]) == "No active…"
    assert len(lines) == 2
    assert len(ANSI_STYLE_PATTERN.sub("", lines[1])) == 10

    if colour:
        assert lines[1].endswith("…\x1b[0m")


def test_narrow_error_shortens_each_plain_line() -> None:
    """Error lines fit a narrow terminal without an enclosing frame."""
    lines = render_error(
        "HCOM error: a long message\nretry later", width=10, current_time="12:34:56"
    )

    assert lines == ["", "HCOM erro…", "retry lat…"]


@pytest.mark.parametrize(
    ("agents", "heading", "totals"),
    [
        (
            [agent("Agent-Tools", "orchestrator"), agent("Agent-Tools", "scout")],
            "WAITING ON YOU",
            "╰─ 1 waiting on you · 0 working ",
        ),
        (
            [agent("Agent-Tools", "scout", status="active")],
            "WORKING",
            "╰─ 0 waiting on you · 1 working ",
        ),
    ],
)
def test_single_group_uses_only_its_heading(
    agents: list[dict], heading: str, totals: str
) -> None:
    """A one-group board omits the other group's heading and counts it as zero."""
    lines = render_board(agents, width=60, current_time="12:34:56")

    assert lines[3].startswith(f"│  {heading}")
    assert len(lines) == 7
    assert lines[-1].startswith(totals)


def test_attention_and_working_groups_use_priority_order() -> None:
    """Blocked teams lead the waiting group, and teams still working go under it."""
    agents = [
        agent("Agent-Tools-partial", "orchestrator", age=70),
        agent("Agent-Tools-idle", "orchestrator", age=90),
        agent("Agent-Tools-idle", "scout", age=80),
        agent("Agent-Tools-blocked", "orchestrator", status="blocked", age=100),
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-blocked", "reviewer", status="active", age=10),
    ]

    lines = board_content(agents)

    assert lines == [
        "WAITING ON YOU",
        "✕ Agent-Tools · blocked  blocked · 2m",
        "● Agent-Tools · idle  needs you",
        "",
        "WORKING",
        "○ Agent-Tools · partial (partial team)  working",
    ]


def test_blocked_waits_sort_before_waiting_team_names() -> None:
    """Blocked waits rank by duration while waiting teams rank by name."""
    agents = [
        agent("Agent-Tools-z-idle", "orchestrator", age=500),
        agent("Agent-Tools-z-idle", "scout", age=500),
        agent("Agent-Tools-a-workers", "scout", age=10),
        agent("Agent-Tools-a-workers", "reviewer", age=10),
        agent("Agent-Tools-a-blocked", "scout", status="blocked", age=30),
        agent("Agent-Tools-z-blocked", "scout", status="blocked", age=120),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "✕ Agent-Tools · z-blocked  blocked · 2m",
        "✕ Agent-Tools · a-blocked  blocked · 30s",
        "● Agent-Tools · z-idle  needs you",
        "",
        "WORKING",
        "○ Agent-Tools · a-workers  working",
    ]


def test_active_and_unread_teams_remain_working_rows() -> None:
    """An active role keeps a team working after unread messages grow old."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="active"),
        agent("Agent-Tools", "scout", unread=1),
        agent("Custom", "orchestrator", age=30),
        agent("Custom", "scout", status="active", age=20),
        agent("Agent-Tools-old", "scout", status="inactive"),
    ]

    lines = board_content(
        agents,
        unread_since={"Agent-Tools-scout": 0},
        now=61,
    )

    assert lines == ["WORKING", "○ Agent-Tools  coordinating", "◌ Custom  checking"]


def test_active_team_becomes_stuck_after_five_minutes_without_events() -> None:
    """A silent active member needs attention once the whole team goes quiet."""
    workers = [
        agent("Agent-Tools", "orchestrator"),
        agent("Agent-Tools", "scout", status="active"),
    ]
    last_activity = {member["name"]: 0 for member in workers}

    assert board_content(workers, last_activity=last_activity, now=299) == [
        "WORKING",
        "◌ Agent-Tools  checking",
    ]
    assert board_content(workers, last_activity=last_activity, now=300) == [
        "WAITING ON YOU",
        "● Agent-Tools  stuck",
    ]


def test_recent_teammate_event_or_missing_event_keeps_team_working() -> None:
    """The board waits for every member's event time before calling a team quiet."""
    workers = [
        agent("Agent-Tools", "orchestrator"),
        agent("Agent-Tools", "scout", status="active"),
    ]

    assert board_content(
        workers,
        last_activity={workers[0]["name"]: 250, workers[1]["name"]: 0},
        now=300,
    ) == ["WORKING", "◌ Agent-Tools  checking"]
    assert board_content(
        workers,
        last_activity={workers[1]["name"]: 0},
        now=300,
    ) == ["WORKING", "◌ Agent-Tools  checking"]


def test_quiet_team_shows_a_known_terminal_reason() -> None:
    """A matched failure adds a reason without changing the stuck symbol."""
    worker = agent("Agent-Tools", "scout", status="active")

    assert board_content(
        [worker],
        last_activity={worker["name"]: 0},
        quiet_reasons={worker["name"]: "model at capacity"},
        now=300,
    ) == ["WAITING ON YOU", "● Agent-Tools  stuck · model at capacity"]


def test_blocked_and_stale_rows_take_priority_over_quiet() -> None:
    """Existing attention states remain visible when a team is also quiet."""
    orchestrator = agent("Agent-Tools", "orchestrator", status="blocked")
    scout = agent("Agent-Tools", "scout", status="active")
    last_activity = {member["name"]: 0 for member in (orchestrator, scout)}

    assert board_content(
        [orchestrator, scout], last_activity=last_activity, now=300
    ) == ["WAITING ON YOU", "✕ Agent-Tools  blocked · 1m"]

    orchestrator["status"] = "active"
    orchestrator.update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )

    assert board_content(
        [orchestrator, scout], last_activity=last_activity, now=300
    ) == ["WAITING ON YOU", "● Agent-Tools  stale"]


def test_mismatched_active_session_needs_attention() -> None:
    """An active agent bound to another transcript makes its team stale."""
    orchestrator = agent("Agent-Tools", "orchestrator", status="active")
    orchestrator.update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )

    assert board_content([orchestrator, agent("Agent-Tools", "scout")]) == [
        "WAITING ON YOU",
        "● Agent-Tools  stale",
    ]


@pytest.mark.parametrize(
    ("session_id", "transcript_path"),
    [
        ("current-session", "/transcripts/current-session.jsonl"),
        (None, "/transcripts/other-session.jsonl"),
        ("current-session", None),
    ],
)
def test_active_session_needs_both_fields_and_a_mismatch(
    session_id: str | None, transcript_path: str | None
) -> None:
    """Matching sessions and incomplete records leave an active team working."""
    orchestrator = agent("Agent-Tools", "orchestrator", status="active")
    orchestrator.update(session_id=session_id, transcript_path=transcript_path)

    assert board_content([orchestrator, agent("Agent-Tools", "scout")]) == [
        "WORKING",
        "○ Agent-Tools  coordinating",
    ]


def test_listening_agent_with_mismatched_session_keeps_team_unchanged() -> None:
    """A listening agent with a mismatched session leaves its team's row unchanged."""
    agents = [agent("Agent-Tools", "orchestrator"), agent("Agent-Tools", "scout")]
    without_session = board_content(agents)

    agents[0].update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )

    assert without_session == ["WAITING ON YOU", "● Agent-Tools  needs you"]
    assert board_content(agents) == without_session


def test_blocked_team_takes_priority_over_stale_agent() -> None:
    """A team with a blocked teammate shows as blocked even when another agent is stale."""
    orchestrator = agent("Agent-Tools", "orchestrator", status="active")
    orchestrator.update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )

    assert board_content(
        [orchestrator, agent("Agent-Tools", "scout", status="blocked")]
    ) == ["WAITING ON YOU", "✕ Agent-Tools  blocked · 1m"]


def test_stale_team_sorts_before_stuck_team() -> None:
    """A stale team appears ahead of a stuck team regardless of name."""
    stale = agent("Agent-Tools-z-stale", "orchestrator", status="active")
    stale.update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )
    stuck = agent("Agent-Tools-a-stuck", "orchestrator", unread=1)
    agents = [
        stale,
        agent("Agent-Tools-z-stale", "scout"),
        stuck,
        agent("Agent-Tools-a-stuck", "scout"),
    ]

    assert board_content(
        agents,
        unread_since={stuck["name"]: 0},
        now=61,
    ) == [
        "WAITING ON YOU",
        "● Agent-Tools · z-stale  stale",
        "● Agent-Tools · a-stuck  stuck",
    ]


def test_unread_team_becomes_stuck_after_the_grace_period() -> None:
    """A listening team needs attention once unread messages have waited over a minute."""
    agents = [
        agent("Agent-Tools", "orchestrator", unread=2, age=0),
        agent("Agent-Tools", "scout", age=0),
    ]
    unread_since = {"Agent-Tools-orchestrator": 10}

    assert board_content(agents, unread_since=unread_since, now=70) == [
        "WORKING",
        "○ Agent-Tools  working",
    ]
    assert board_content(agents, unread_since=unread_since, now=71) == [
        "WAITING ON YOU",
        "● Agent-Tools  stuck",
    ]


def test_lone_listening_agent_with_old_unread_messages_is_stuck() -> None:
    """A partial team with unread messages also needs attention."""
    agents = [agent("Agent-Tools", "scout", unread=1)]

    assert board_content(
        agents,
        unread_since={"Agent-Tools-scout": 0},
        now=61,
    ) == ["WAITING ON YOU", "● Agent-Tools  stuck"]


def test_stuck_team_sorts_with_attention_and_uses_attention_colour() -> None:
    """Stuck teams appear after blocked teams and before working teams."""
    agents = [
        agent("Agent-Tools-working", "scout", status="active"),
        agent("Agent-Tools-stuck", "orchestrator", unread=1),
        agent("Agent-Tools-stuck", "scout"),
        agent("Agent-Tools-blocked", "orchestrator", unread=1),
        agent("Agent-Tools-blocked", "scout", status="blocked"),
    ]

    lines = board_content(
        agents,
        colour=True,
        unread_since={
            "Agent-Tools-blocked-orchestrator": 0,
            "Agent-Tools-stuck-orchestrator": 0,
        },
        now=61,
    )

    assert lines[0] == "\x1b[2mWAITING ON YOU\x1b[0m"
    assert lines[1].startswith("\x1b[31m✕\x1b[0m ")
    assert "\x1b[31mblocked" in lines[1]
    assert lines[2].startswith("\x1b[35m●\x1b[0m ")
    assert lines[2].endswith("\x1b[35mstuck\x1b[0m")
    assert lines[4] == "\x1b[2mWORKING\x1b[0m"


def test_unread_tracking_resets_when_messages_clear_or_agent_activates() -> None:
    """Reading messages or becoming active restarts the grace period."""
    listener = agent("Agent-Tools", "orchestrator", unread=1)
    partner = agent("Agent-Tools", "scout")
    unread_since = {}

    _update_unread_since([listener, partner], unread_since, 10)
    assert board_content([listener, partner], unread_since=unread_since, now=71)[
        1
    ].startswith("● Agent-Tools  stuck")

    listener["unread_count"] = 0
    _update_unread_since([listener, partner], unread_since, 72)
    assert unread_since == {}
    assert board_content([listener, partner], unread_since=unread_since, now=72) == [
        "WAITING ON YOU",
        "● Agent-Tools  needs you",
    ]

    listener["unread_count"] = 1
    _update_unread_since([listener, partner], unread_since, 73)
    assert board_content([listener, partner], unread_since=unread_since, now=73) == [
        "WORKING",
        "○ Agent-Tools  working",
    ]

    listener["status"] = "active"
    _update_unread_since([listener, partner], unread_since, 74)
    assert unread_since == {}
    assert board_content([listener, partner], unread_since=unread_since, now=74) == [
        "WORKING",
        "○ Agent-Tools  coordinating",
    ]


def test_launching_team_stays_working_with_old_unread_messages() -> None:
    """A launching member keeps an otherwise stuck team in the working group."""
    agents = [
        agent("Agent-Tools", "orchestrator", unread=1),
        agent("Agent-Tools", "scout", status="launching"),
    ]

    assert board_content(
        agents,
        unread_since={"Agent-Tools-orchestrator": 0},
        now=61,
    ) == ["WORKING", "◇ Agent-Tools  starting"]


@pytest.mark.parametrize(
    ("role", "label", "symbol", "partner_role"),
    [
        ("implementer", "implementing", "▶", "orchestrator"),
        ("reviewer", "reviewing", "◎", "orchestrator"),
        ("scout", "checking", "◌", "orchestrator"),
        ("orchestrator", "coordinating", "○", "scout"),
    ],
)
def test_active_role_names_working_team(
    role: str, label: str, symbol: str, partner_role: str
) -> None:
    """Each recognised active role gives the team its activity label."""
    agents = [
        agent("Agent-Tools", role, status="active"),
        agent("Agent-Tools", partner_role, status="listening"),
    ]

    assert board_content(agents)[1] == f"{symbol} Agent-Tools  {label}"


@pytest.mark.parametrize(
    ("roles", "label", "symbol"),
    [
        (["orchestrator", "scout"], "checking", "◌"),
        (["scout", "reviewer"], "reviewing", "◎"),
        (["reviewer", "implementer"], "implementing", "▶"),
    ],
)
def test_active_role_priority_names_working_team(
    roles: list[str], label: str, symbol: str
) -> None:
    """The highest-priority active role names a team with concurrent work."""
    agents = [agent("Agent-Tools", role, status="active") for role in roles]

    assert board_content(agents)[1] == f"{symbol} Agent-Tools  {label}"


def test_launching_team_is_starting_without_active_members() -> None:
    """Launching takes precedence over the working fallback."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="launching"),
        agent("Agent-Tools", "scout", status="listening"),
    ]

    assert board_content(agents)[1] == "◇ Agent-Tools  starting"


def test_active_role_takes_priority_over_launching_member() -> None:
    """An active role names the team while another member launches."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="launching"),
        agent("Agent-Tools", "scout", status="active"),
    ]

    assert board_content(agents)[1] == "◌ Agent-Tools  checking"


def test_unrecognised_active_role_keeps_working_fallback() -> None:
    """An active agent with no known role cannot choose the team label."""
    untagged_agent = agent("Agent-Tools", None, status="active")
    untagged_agent["name"] = "Agent-Tools"
    untagged_agent["tag"] = None
    agents = [
        agent("Agent-Tools", "orchestrator"),
        untagged_agent,
    ]

    assert board_content(agents)[1] == "○ Agent-Tools  working"


def test_active_partial_team_keeps_suffix() -> None:
    """A lone active agent shows its work and remains marked as partial."""
    agents = [agent("Agent-Tools", "implementer", status="active")]

    assert board_content(agents)[1] == "▶ Agent-Tools (partial team)  implementing"


def test_labelled_learner_team_shares_one_complete_row() -> None:
    """An orchestrator and scout sharing a workflow label form one complete row."""
    agents = [
        agent("Agent-Tools-learn-claude", "orchestrator"),
        agent("Agent-Tools-learn-claude", "scout", status="active"),
    ]

    assert board_content(agents) == [
        "WORKING",
        "◌ Agent-Tools · learn-claude  checking",
    ]


def test_lone_labelled_learner_scout_is_partial() -> None:
    """A workflow scout without its orchestrator is shown as a partial team."""
    agents = [agent("Agent-Tools-learn-claude", "scout", status="active")]

    assert board_content(agents) == [
        "WORKING",
        "◌ Agent-Tools · learn-claude (partial team)  checking",
    ]


def test_standard_team_with_learner_in_its_label_stays_complete() -> None:
    """A team whose label contains "learner" is complete like any other team."""
    agents = [
        agent("Agent-Tools-learner-ui", "orchestrator"),
        agent("Agent-Tools-learner-ui", "implementer"),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "● Agent-Tools · learner-ui  needs you",
    ]


def test_repository_name_containing_learner_keeps_standard_team_rule() -> None:
    """A repository name containing "learner" is shown like any other."""
    agents = [
        agent("e-learner-app-board", "orchestrator", directory="/work/e-learner-app"),
        agent("e-learner-app-board", "implementer", directory="/work/e-learner-app"),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "● e-learner-app · board  needs you",
    ]


def test_single_live_agent_joins_dimmed_working_group() -> None:
    """An incomplete one-agent team is marked as partial and dimmed."""
    agents = [
        agent("Agent-Tools-alone", "scout", age=3600),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = board_content(agents, colour=True)

    assert ANSI_STYLE_PATTERN.sub("", lines[1]).startswith("● Agent-Tools · idle")
    assert lines[1].startswith("\x1b[35m●\x1b[0m \x1b[97mAgent-Tools")
    assert "\x1b[2m · idle\x1b[0m" in lines[1]
    assert lines[4].startswith("\x1b[2m○ Agent-Tools · alone (partial team)")
    assert lines[4].endswith("working\x1b[0m")


def test_directory_normalisation_without_idle_age() -> None:
    """Folder names become tag-style labels while waiting rows omit ages."""
    agents = [
        agent("Lew-Timer", "orchestrator", directory="/work/Lew Timer!", age=119),
        agent("Lew-Timer", "reviewer", directory="/work/Lew Timer!", age=40),
    ]

    assert board_content(agents)[1] == "● Lew-Timer  needs you"


def test_workers_in_nested_directory_share_the_team() -> None:
    """A worker in a package folder stays with the repo-root orchestrator."""
    agents = [
        agent("Agent-Tools-board", "orchestrator", age=60),
        agent(
            "Agent-Tools-board",
            "scout",
            directory="/work/Agent Tools/packages/agent-board",
            age=40,
        ),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "● Agent-Tools · board  needs you",
    ]


def test_worktree_members_use_the_main_checkout_folder() -> None:
    """A managed worktree shows the repository and task label separately."""
    agents = [
        agent(
            "Agents-quick-library-update",
            "orchestrator",
            directory="/home/user/.agents/worktrees/prj_one/tsk_one",
        ),
        agent(
            "Agents-quick-library-update",
            "scout",
            directory="/home/user/.agents/worktrees/prj_one/tsk_one/src",
        ),
    ]

    for member in agents:
        member["main_checkout"] = "/work/Agents"

    assert board_content(agents)[1] == "● Agents · quick-library-update  needs you"


def test_longest_matching_folder_names_the_repo() -> None:
    """A short matching subfolder cannot replace the repository name."""
    agents = [
        agent(
            "Agent-Tools-board",
            "scout",
            directory="/work/Agent Tools/packages/Agent",
        ),
        agent("Agent-Tools-board", "orchestrator"),
    ]

    assert board_content(agents)[1] == "● Agent-Tools · board  needs you"


def test_workers_without_orchestrator_are_working() -> None:
    """A team with two workers is working even without an orchestrator."""
    agents = [
        agent("Agent-Tools-workers", "scout", age=100),
        agent("Agent-Tools-workers", "reviewer", age=80),
    ]

    assert board_content(agents, colour=True)[1].startswith(
        "\x1b[2m○ Agent-Tools · workers"
    )


def test_distinct_prefixes_with_the_same_label_are_both_shown() -> None:
    """Teams with equal display labels remain separate working rows."""
    agents = [
        agent("Agent-Tools-a_b", "scout"),
        agent("Agent-Tools-a_b", "reviewer"),
        agent("Agent-Tools-a--b", "scout"),
        agent("Agent-Tools-a--b", "reviewer"),
    ]

    assert board_content(agents) == [
        "WORKING",
        "○ Agent-Tools · a-b  working",
        "○ Agent-Tools · a-b  working",
    ]


def test_lone_blocked_agent_keeps_age_order_and_red_status() -> None:
    """A blocked lone agent keeps its wait and the attention colours."""
    agents = [
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = board_content(agents, colour=True)

    assert lines[1].startswith("\x1b[31m✕\x1b[0m \x1b[97mAgent-Tools")
    assert "\x1b[2m · blocked\x1b[0m" in lines[1]
    assert lines[1].endswith("\x1b[31mblocked · 2m\x1b[0m")
    assert lines[2].startswith("\x1b[35m●\x1b[0m \x1b[97mAgent-Tools")
    assert "\x1b[2m · idle\x1b[0m" in lines[2]
    assert lines[2].endswith("\x1b[35mneeds you\x1b[0m")


def test_working_rows_are_fully_dimmed_when_colour_is_on() -> None:
    """Active, unread, and multi-worker teams use the same dim working style."""
    agents = [
        agent("Agent-Tools-active", "orchestrator", status="active"),
        agent("Agent-Tools-active", "scout"),
        agent("Agent-Tools-unread", "orchestrator"),
        agent("Agent-Tools-unread", "scout", unread=1),
        agent("Agent-Tools-workers", "scout"),
        agent("Agent-Tools-workers", "reviewer"),
    ]

    lines = board_content(agents, colour=True)

    assert lines[0] == "\x1b[2mWORKING\x1b[0m"
    # The team label from each row: the text between the repository and the status.
    assert [
        ANSI_STYLE_PATTERN.sub("", line).split(" · ")[1].split("  ")[0]
        for line in lines[1:]
    ] == ["active", "unread", "workers"]
    assert all(line.startswith("\x1b[2m○ Agent-Tools") for line in lines[1:])
    assert all(line.endswith("\x1b[0m") for line in lines[1:])


def test_no_active_teams_shows_one_message_inside_the_frame() -> None:
    """An empty or inactive listing shows only the message inside the frame."""
    assert board_content([]) == ["No active teams"]
    assert board_content([], colour=True) == ["\x1b[2mNo active teams\x1b[0m"]
    assert board_content([agent("Agent-Tools", "scout", status="inactive")]) == [
        "No active teams",
    ]


def test_unfamiliar_tag_without_role_keeps_raw_prefix() -> None:
    """An unfamiliar tag remains on the board as its own team."""
    entry = agent("CustomTag", None, status="active", age=30)

    assert board_content([entry])[1] == "○ CustomTag (partial team)  working"


def test_missing_tag_uses_agent_name_as_team_prefix() -> None:
    """An agent without a tag remains visible under its own name."""
    for missing_tag in (None, ""):
        entry = agent("Agent-Tools-standalone-lamo", None, age=30)
        entry["name"] = "Agent-Tools-standalone-lamo"
        entry["tag"] = missing_tag

        assert board_content([entry])[1] == (
            "○ Agent-Tools · standalone-lamo (partial team)  working"
        )

    entry = agent("Agent-Tools-standalone-lamo", None, age=30)
    entry["name"] = "Agent-Tools-standalone-lamo"
    del entry["tag"]

    assert board_content([entry])[1] == (
        "○ Agent-Tools · standalone-lamo (partial team)  working"
    )


@pytest.mark.parametrize("width", [29, 30, 80, 161])
def test_quota_stacks_below_frame_without_changing_compact_rows(width: int) -> None:
    """The panel starts below the teams frame from 30 through 161 columns."""
    board_lines = render_board([], width=width, current_time="12:34:56")
    panel_widths = []

    def render_panel(panel_width: int) -> list[str]:
        """Record the width selected for the quota panel."""
        panel_widths.append(panel_width)
        return ["Quota", "checking…"]

    combined = place_quota_panel(board_lines, render_panel, width=width)

    if width < 30:
        assert combined == board_lines
        assert panel_widths == []
    else:
        assert combined == [*board_lines, "", "Quota", "checking…"]
        assert panel_widths == [width]


@pytest.mark.parametrize("width", [162, 200])
def test_quota_uses_the_spare_width_beside_the_full_board(width: int) -> None:
    """The side panel grows with the terminal until it reaches 80 columns."""
    board_lines = render_board([], width=width, current_time="12:34:56")
    panel_widths = []

    def render_panel(panel_width: int) -> list[str]:
        """Draw a line that fills the width selected by the board."""
        panel_widths.append(panel_width)
        return ["Quota", "x" * panel_width]

    combined = place_quota_panel(board_lines, render_panel, width=width)
    panel_width = min(width - 82, 80)

    assert panel_widths == [panel_width]
    assert combined[0] == ""
    assert combined[1] == board_lines[1] + "  Quota"
    assert combined[2] == board_lines[2] + "  " + "x" * panel_width
    assert all(len(ANSI_STYLE_PATTERN.sub("", line)) <= width for line in combined)


@pytest.mark.parametrize("colour", [False, True])
def test_taller_teams_column_keeps_the_quota_in_place(colour: bool) -> None:
    """A short quota panel leaves the lower teams rows untouched, coloured or plain."""
    agents = [
        agent(f"Agent-Tools-{index}", "scout", status="active") for index in range(5)
    ]
    board_lines = render_board(
        agents, width=162, current_time="12:34:56", colour=colour
    )

    combined = place_quota_panel(board_lines, lambda width: ["Quota"], width=162)

    assert len(combined) == len(board_lines)
    assert combined[1] == board_lines[1] + "  Quota"
    assert all(
        ANSI_STYLE_PATTERN.sub("", line) == ANSI_STYLE_PATTERN.sub("", board_line)
        for line, board_line in zip(combined[2:], board_lines[2:])
    )


def test_taller_quota_column_pads_after_the_teams_frame() -> None:
    """Quota rows continue in the right column when the teams frame ends."""
    board_lines = render_board([], width=162, current_time="12:34:56")
    panel_lines = ["Quota", *[f"account {index}" for index in range(8)]]

    combined = place_quota_panel(board_lines, lambda width: panel_lines, width=162)

    assert len(combined) == len(panel_lines) + 1
    assert combined[len(board_lines)] == " " * 82 + panel_lines[len(board_lines) - 1]
    assert combined[-1] == " " * 82 + panel_lines[-1]


def test_quota_heading_lines_up_beside_an_hcom_error() -> None:
    """The quota heading sits beside the top edge of an hcom error frame."""
    error_lines = render_error("listing failed", width=162, current_time="12:34:56")

    combined = place_quota_panel(error_lines, lambda width: ["Quota"], width=162)

    assert combined[0] == ""
    assert combined[1] == error_lines[1] + "  Quota"


def test_quota_layout_switches_when_the_terminal_crosses_162_columns() -> None:
    """Every redraw places the same panel according to the current width."""
    panel_lines = ["Quota", "checking…"]
    layouts = []

    for width in [161, 162, 161]:
        board_lines = render_board([], width=width, current_time="12:34:56")
        layouts.append(
            place_quota_panel(board_lines, lambda panel_width: panel_lines, width=width)
        )

    assert layouts[0] == layouts[2]
    assert layouts[0][-2:] == panel_lines
    assert layouts[1][1].endswith("  Quota")
    assert layouts[1][-2:] != panel_lines
