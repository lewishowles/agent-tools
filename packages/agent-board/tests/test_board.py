"""Check board decisions against representative HCOM listings."""

import re

import pytest
from agent_board.board import (
    ANSI_STYLE_PATTERN,
    quiet_team_members,
    render_board,
    render_error,
)
from agent_board.cli import _update_unread_since


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
    """Return the content rows as drawn inside the frame, including headings."""
    quiet_members = (
        quiet_team_members(agents, last_activity, now)
        if last_activity is not None
        else {}
    )
    framed = render_board(
        agents,
        width=80,
        current_time="12:34:56",
        colour=colour,
        unread_since=unread_since,
        last_activity=last_activity,
        quiet_members=quiet_members,
        quiet_reasons=quiet_reasons,
        now=now,
        phase_since=phase_since,
    )
    rows = []

    for row in framed[3:-2]:
        content = re.sub(r"^(?:\x1b\[[0-9;]*m)?│(?:\x1b\[[0-9;]*m)?  ", "", row)
        content = re.sub(r"  (?:\x1b\[[0-9;]*m)?│(?:\x1b\[[0-9;]*m)?$", "", content)
        rows.append(content.rstrip())

    return rows


def test_phase_timer_starts_as_an_estimate_and_resets_when_the_status_changes() -> None:
    """Only a phase first seen by the board carries an estimated start."""
    phase_since = {}
    waiting = [agent("Agent-Tools", "orchestrator"), agent("Agent-Tools", "scout")]
    checking = [
        agent("Agent-Tools", "orchestrator"),
        agent("Agent-Tools", "scout", status="active"),
    ]

    assert board_content(waiting, now=100, phase_since=phase_since)[1] == (
        "● needs you · 0s+  Agent-Tools"
    )
    assert board_content(waiting, now=172, phase_since=phase_since)[1] == (
        "● needs you · 1m+  Agent-Tools"
    )
    assert board_content(checking, now=180, phase_since=phase_since)[1] == (
        "◌ checking · 0s  Agent-Tools"
    )
    assert board_content(checking, now=245, phase_since=phase_since)[1] == (
        "◌ checking · 1m  Agent-Tools"
    )
    assert board_content(waiting, now=250, phase_since=phase_since)[1] == (
        "● needs you · 0s  Agent-Tools"
    )


def test_disappearing_team_gets_a_new_estimated_baseline() -> None:
    """A returning team cannot reuse its earlier observed phase time."""
    phase_since = {}
    members = [agent("Agent-Tools", "scout", status="active")]

    assert board_content(members, now=100, phase_since=phase_since)[1] == (
        "◌ checking · 0s+  Agent-Tools (partial team)"
    )
    assert board_content([], now=120, phase_since=phase_since) == ["No active teams"]
    assert board_content(members, now=200, phase_since=phase_since)[1] == (
        "◌ checking · 0s+  Agent-Tools (partial team)"
    )


def test_blocked_row_uses_hcom_age_instead_of_a_phase_timer() -> None:
    """The blocked row keeps HCOM's longest wait without an estimate suffix."""
    members = [agent("Agent-Tools", "scout", status="blocked", age=240)]

    assert board_content(members, now=100, phase_since={})[1] == (
        "✕ blocked · 4m  Agent-Tools"
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
        == "● stuck · 5m  Agent-Tools"
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
        == "● stuck · 1m  Agent-Tools"
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
        == "● stuck · model at capacity · 5m+  Agent-Tools"
    )
    assert (
        board_content(
            members,
            last_activity=last_activity,
            quiet_reasons={worker: "rate limited"},
            now=362,
            phase_since=phase_since,
        )[1]
        == "● stuck · rate limited · 6m+  Agent-Tools"
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
    assert lines[4].startswith("│  ● needs you")
    assert lines[5] == lines[2]
    assert lines[6] == "│  WORKING" + " " * 47 + "  │"
    assert lines[7].startswith("│  ○ coordinating")
    assert lines[-2] == lines[2]
    assert lines[-1].startswith("╰─ 1 waiting on you · 1 working ")
    assert lines[-1].endswith("─╯")
    assert all(len(line) == 60 for line in lines[1:])


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
    """A long team name ends inside the capped frame with an ellipsis."""
    agents = [agent("Agent-Tools-" + "long-" * 20, "scout", status="active")]

    lines = render_board(agents, width=200, current_time="12:34:56")

    assert lines[4].endswith("…  │")
    assert all(len(line) == 80 for line in lines[1:])


@pytest.mark.parametrize(
    ("colour", "expected_rows"),
    [
        (
            False,
            [
                "WAITING ON YOU",
                "✕ blocked · 2m  Agent-Tools · blocked",
                "",
                "WORKING",
                "◌ checking      Agent-Tools · working (partial team)",
            ],
        ),
        (
            True,
            [
                "\x1b[2mWAITING ON YOU\x1b[0m",
                "\x1b[31m✕ blocked · 2m\x1b[0m  \x1b[97mAgent-Tools\x1b[0m\x1b[2m · blocked\x1b[0m",
                "",
                "\x1b[2mWORKING\x1b[0m",
                "\x1b[2m◌ checking      Agent-Tools · working (partial team)\x1b[0m",
            ],
        ),
    ],
)
def test_frame_pads_coloured_and_plain_rows_to_the_same_width(
    colour: bool, expected_rows: list[str]
) -> None:
    """Colour codes do not move the right edge of a blocked or working row."""
    agents = [
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-working", "scout", status="active"),
    ]

    lines = render_board(agents, width=70, current_time="01:02:03", colour=colour)

    assert board_content(agents, colour=colour) == expected_rows
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
    assert plain_lines[4].startswith("│  ✕ blocked · 1m")
    assert plain_lines[4].endswith("…  │")
    assert plain_lines[7].endswith("…  │")
    assert plain_lines[-1] == "╰─ 1 waiting on you · 1 wo… ─╯"
    assert all(len(line) == 30 for line in plain_lines[1:])

    if colour:
        assert "…\x1b[0m" in lines[4]
        assert "…\x1b[0m" in lines[7]
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
        "✕ blocked · 1m  Agent-Tools …",
        "",
        "◌ checking      Agent-Tools …",
    ]
    assert all(len(line) <= 29 for line in plain_lines)

    if colour:
        assert lines[1].endswith("…\x1b[0m")
        assert lines[3].endswith("…\x1b[0m")
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


def test_attention_and_working_groups_use_priority_and_align_statuses() -> None:
    """Blocked teams come first, and every row shares a status column."""
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
        "✕ blocked · 2m  Agent-Tools · blocked",
        "● needs you     Agent-Tools · idle",
        "",
        "WORKING",
        "○ working       Agent-Tools · partial (partial team)",
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
        "✕ blocked · 2m   Agent-Tools · z-blocked",
        "✕ blocked · 30s  Agent-Tools · a-blocked",
        "● needs you      Agent-Tools · z-idle",
        "",
        "WORKING",
        "○ working        Agent-Tools · a-workers",
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

    assert lines == ["WORKING", "○ coordinating  Agent-Tools", "◌ checking      Custom"]


def test_active_team_becomes_stuck_after_five_minutes_without_events() -> None:
    """A silent active member needs attention once the whole team goes quiet."""
    workers = [
        agent("Agent-Tools", "orchestrator"),
        agent("Agent-Tools", "scout", status="active"),
    ]
    last_activity = {member["name"]: 0 for member in workers}

    assert board_content(workers, last_activity=last_activity, now=299) == [
        "WORKING",
        "◌ checking  Agent-Tools",
    ]
    assert board_content(workers, last_activity=last_activity, now=300) == [
        "WAITING ON YOU",
        "● stuck  Agent-Tools",
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
    ) == ["WORKING", "◌ checking  Agent-Tools"]
    assert board_content(
        workers,
        last_activity={workers[1]["name"]: 0},
        now=300,
    ) == ["WORKING", "◌ checking  Agent-Tools"]


def test_quiet_team_shows_a_known_terminal_reason() -> None:
    """A matched failure adds a reason without changing the stuck symbol."""
    worker = agent("Agent-Tools", "scout", status="active")

    assert board_content(
        [worker],
        last_activity={worker["name"]: 0},
        quiet_reasons={worker["name"]: "model at capacity"},
        now=300,
    ) == ["WAITING ON YOU", "● stuck · model at capacity  Agent-Tools"]


def test_blocked_and_stale_rows_take_priority_over_quiet() -> None:
    """Existing attention states remain visible when a team is also quiet."""
    orchestrator = agent("Agent-Tools", "orchestrator", status="blocked")
    scout = agent("Agent-Tools", "scout", status="active")
    last_activity = {member["name"]: 0 for member in (orchestrator, scout)}

    assert board_content(
        [orchestrator, scout], last_activity=last_activity, now=300
    ) == ["WAITING ON YOU", "✕ blocked · 1m  Agent-Tools"]

    orchestrator["status"] = "active"
    orchestrator.update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )

    assert board_content(
        [orchestrator, scout], last_activity=last_activity, now=300
    ) == ["WAITING ON YOU", "● stale  Agent-Tools"]


def test_mismatched_active_session_needs_attention() -> None:
    """An active agent bound to another transcript makes its team stale."""
    orchestrator = agent("Agent-Tools", "orchestrator", status="active")
    orchestrator.update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )

    assert board_content([orchestrator, agent("Agent-Tools", "scout")]) == [
        "WAITING ON YOU",
        "● stale  Agent-Tools",
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
        "○ coordinating  Agent-Tools",
    ]


def test_listening_agent_with_mismatched_session_keeps_team_unchanged() -> None:
    """A listening agent with a mismatched session leaves its team's row unchanged."""
    agents = [agent("Agent-Tools", "orchestrator"), agent("Agent-Tools", "scout")]
    without_session = board_content(agents)

    agents[0].update(
        session_id="current-session",
        transcript_path="/transcripts/other-session.jsonl",
    )

    assert without_session == ["WAITING ON YOU", "● needs you  Agent-Tools"]
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
    ) == ["WAITING ON YOU", "✕ blocked · 1m  Agent-Tools"]


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
        "● stale  Agent-Tools · z-stale",
        "● stuck  Agent-Tools · a-stuck",
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
        "○ working  Agent-Tools",
    ]
    assert board_content(agents, unread_since=unread_since, now=71) == [
        "WAITING ON YOU",
        "● stuck  Agent-Tools",
    ]


def test_lone_listening_agent_with_old_unread_messages_is_stuck() -> None:
    """A partial team with unread messages also needs attention."""
    agents = [agent("Agent-Tools", "scout", unread=1)]

    assert board_content(
        agents,
        unread_since={"Agent-Tools-scout": 0},
        now=61,
    ) == ["WAITING ON YOU", "● stuck  Agent-Tools"]


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
    assert lines[1].startswith("\x1b[31m✕ blocked")
    assert lines[2].startswith("\x1b[35m● stuck")
    assert lines[4] == "\x1b[2mWORKING\x1b[0m"


def test_unread_tracking_resets_when_messages_clear_or_agent_activates() -> None:
    """Reading messages or becoming active restarts the grace period."""
    listener = agent("Agent-Tools", "orchestrator", unread=1)
    partner = agent("Agent-Tools", "scout")
    unread_since = {}

    _update_unread_since([listener, partner], unread_since, 10)
    assert board_content([listener, partner], unread_since=unread_since, now=71)[
        1
    ].startswith("● stuck")

    listener["unread_count"] = 0
    _update_unread_since([listener, partner], unread_since, 72)
    assert unread_since == {}
    assert board_content([listener, partner], unread_since=unread_since, now=72) == [
        "WAITING ON YOU",
        "● needs you  Agent-Tools",
    ]

    listener["unread_count"] = 1
    _update_unread_since([listener, partner], unread_since, 73)
    assert board_content([listener, partner], unread_since=unread_since, now=73) == [
        "WORKING",
        "○ working  Agent-Tools",
    ]

    listener["status"] = "active"
    _update_unread_since([listener, partner], unread_since, 74)
    assert unread_since == {}
    assert board_content([listener, partner], unread_since=unread_since, now=74) == [
        "WORKING",
        "○ coordinating  Agent-Tools",
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
    ) == ["WORKING", "◇ starting  Agent-Tools"]


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

    assert board_content(agents)[1] == f"{symbol} {label}  Agent-Tools"


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

    assert board_content(agents)[1] == f"{symbol} {label}  Agent-Tools"


def test_launching_team_is_starting_without_active_members() -> None:
    """Launching takes precedence over the working fallback."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="launching"),
        agent("Agent-Tools", "scout", status="listening"),
    ]

    assert board_content(agents)[1] == "◇ starting  Agent-Tools"


def test_active_role_takes_priority_over_launching_member() -> None:
    """An active role names the team while another member launches."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="launching"),
        agent("Agent-Tools", "scout", status="active"),
    ]

    assert board_content(agents)[1] == "◌ checking  Agent-Tools"


def test_unrecognised_active_role_keeps_working_fallback() -> None:
    """An active agent with no known role cannot choose the team label."""
    untagged_agent = agent("Agent-Tools", None, status="active")
    untagged_agent["name"] = "Agent-Tools"
    untagged_agent["tag"] = None
    agents = [
        agent("Agent-Tools", "orchestrator"),
        untagged_agent,
    ]

    assert board_content(agents)[1] == "○ working  Agent-Tools"


def test_active_partial_team_keeps_suffix() -> None:
    """A lone active agent shows its work and remains marked as partial."""
    agents = [agent("Agent-Tools", "implementer", status="active")]

    assert board_content(agents)[1] == "▶ implementing  Agent-Tools (partial team)"


def test_labelled_learner_team_shares_one_complete_row() -> None:
    """An orchestrator and scout sharing a workflow label form one complete row."""
    agents = [
        agent("Agent-Tools-learn-claude", "orchestrator"),
        agent("Agent-Tools-learn-claude", "scout", status="active"),
    ]

    assert board_content(agents) == [
        "WORKING",
        "◌ checking  Agent-Tools · learn-claude",
    ]


def test_lone_labelled_learner_scout_is_partial() -> None:
    """A workflow scout without its orchestrator is shown as a partial team."""
    agents = [agent("Agent-Tools-learn-claude", "scout", status="active")]

    assert board_content(agents) == [
        "WORKING",
        "◌ checking  Agent-Tools · learn-claude (partial team)",
    ]


def test_standard_team_with_learner_in_its_label_stays_complete() -> None:
    """A team whose label contains "learner" is complete like any other team."""
    agents = [
        agent("Agent-Tools-learner-ui", "orchestrator"),
        agent("Agent-Tools-learner-ui", "implementer"),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "● needs you  Agent-Tools · learner-ui",
    ]


def test_repository_name_containing_learner_keeps_standard_team_rule() -> None:
    """A repository name containing "learner" is shown like any other."""
    agents = [
        agent("e-learner-app-board", "orchestrator", directory="/work/e-learner-app"),
        agent("e-learner-app-board", "implementer", directory="/work/e-learner-app"),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "● needs you  e-learner-app · board",
    ]


def test_single_live_agent_joins_dimmed_working_group() -> None:
    """An incomplete one-agent team is marked as partial and dimmed."""
    agents = [
        agent("Agent-Tools-alone", "scout", age=3600),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = board_content(agents, colour=True)

    assert lines == [
        "\x1b[2mWAITING ON YOU\x1b[0m",
        "\x1b[35m● needs you\x1b[0m  \x1b[97mAgent-Tools\x1b[0m\x1b[2m · idle\x1b[0m",
        "",
        "\x1b[2mWORKING\x1b[0m",
        "\x1b[2m○ working    Agent-Tools · alone (partial team)\x1b[0m",
    ]


def test_directory_normalisation_without_idle_age() -> None:
    """Folder names become tag-style labels while waiting rows omit ages."""
    agents = [
        agent("Lew-Timer", "orchestrator", directory="/work/Lew Timer!", age=119),
        agent("Lew-Timer", "reviewer", directory="/work/Lew Timer!", age=40),
    ]

    assert board_content(agents)[1] == "● needs you  Lew-Timer"


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
        "● needs you  Agent-Tools · board",
    ]


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

    assert board_content(agents)[1] == "● needs you  Agent-Tools · board"


def test_workers_without_orchestrator_are_working() -> None:
    """A team with two workers is working even without an orchestrator."""
    agents = [
        agent("Agent-Tools-workers", "scout", age=100),
        agent("Agent-Tools-workers", "reviewer", age=80),
    ]

    assert board_content(agents, colour=True)[1] == (
        "\x1b[2m○ working  Agent-Tools · workers\x1b[0m"
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
        "○ working  Agent-Tools · a-b",
        "○ working  Agent-Tools · a-b",
    ]


def test_lone_blocked_agent_keeps_age_order_and_red_status() -> None:
    """A blocked lone agent keeps its wait and the attention colours."""
    agents = [
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = board_content(agents, colour=True)

    assert lines[1] == (
        "\x1b[31m✕ blocked · 2m\x1b[0m  \x1b[97mAgent-Tools\x1b[0m\x1b[2m · blocked\x1b[0m"
    )
    assert lines[2] == (
        "\x1b[35m● needs you   \x1b[0m  \x1b[97mAgent-Tools\x1b[0m\x1b[2m · idle\x1b[0m"
    )


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

    assert board_content(agents, colour=True) == [
        "\x1b[2mWORKING\x1b[0m",
        "\x1b[2m○ coordinating  Agent-Tools · active\x1b[0m",
        "\x1b[2m○ working       Agent-Tools · unread\x1b[0m",
        "\x1b[2m○ working       Agent-Tools · workers\x1b[0m",
    ]


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

    assert board_content([entry])[1] == "○ working  CustomTag (partial team)"


def test_missing_tag_uses_agent_name_as_team_prefix() -> None:
    """An agent without a tag remains visible under its own name."""
    for missing_tag in (None, ""):
        entry = agent("Agent-Tools-standalone-lamo", None, age=30)
        entry["name"] = "Agent-Tools-standalone-lamo"
        entry["tag"] = missing_tag

        assert board_content([entry])[1] == (
            "○ working  Agent-Tools · standalone-lamo (partial team)"
        )

    entry = agent("Agent-Tools-standalone-lamo", None, age=30)
    entry["name"] = "Agent-Tools-standalone-lamo"
    del entry["tag"]

    assert board_content([entry])[1] == (
        "○ working  Agent-Tools · standalone-lamo (partial team)"
    )
