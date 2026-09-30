"""Check board decisions against representative HCOM listings."""

import pytest
from agent_board.board import ANSI_STYLE_PATTERN, render_board, render_error


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
        "status": status,
        "status_age_seconds": age,
        "tag": f"{prefix}-{role}" if role else prefix,
        "unread_count": unread,
    }


def board_content(agents: list[dict], *, colour: bool = False) -> list[str]:
    """Return the content rows as drawn inside the frame, including headings."""
    framed = render_board(agents, width=80, current_time="12:34:56", colour=colour)
    return [row[3:-3].rstrip() for row in framed[3:-2]]


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
    assert lines[5] == "│  WORKING" + " " * 47 + "  │"
    assert lines[6].startswith("│  ○ coordinating")
    assert lines[-2] == lines[2]
    assert lines[-1].startswith("╰─ 1 waiting on you · 1 working ")
    assert lines[-1].endswith("─╯")
    assert all(len(line) == 60 for line in lines[1:])


@pytest.mark.parametrize(
    ("colour", "expected_rows"),
    [
        (
            False,
            [
                "WAITING ON YOU",
                "✕ blocked · 2m  Agent-Tools · blocked",
                "WORKING",
                "◌ checking      Agent-Tools · working (partial team)",
            ],
        ),
        (
            True,
            [
                "WAITING ON YOU",
                "\x1b[31m✕ blocked · 2m\x1b[0m  \x1b[97mAgent-Tools · blocked\x1b[0m",
                "WORKING",
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

    assert [line[3:-3].rstrip() for line in lines[3:-2]] == expected_rows
    assert all(len(ANSI_STYLE_PATTERN.sub("", line)) == 70 for line in lines[1:])


@pytest.mark.parametrize("colour", [False, True])
def test_empty_board_keeps_message_inside_frame(colour: bool) -> None:
    """An empty board pads its message and shows zero counts in either colour mode."""
    lines = render_board([], width=50, current_time="09:08:07", colour=colour)

    assert len(lines) == 6
    assert lines[2] == "│" + " " * 48 + "│"
    assert lines[3].startswith("│  ")
    assert "No active teams" in lines[3]
    assert lines[-2] == lines[2]
    assert lines[-1].startswith("╰─ 0 waiting on you · 0 working ")
    assert all(len(ANSI_STYLE_PATTERN.sub("", line)) == 50 for line in lines[1:])


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
    assert plain_lines[6].endswith("…  │")
    assert plain_lines[-1] == "╰─ 1 waiting on you · 1 wo… ─╯"
    assert all(len(line) == 30 for line in plain_lines[1:])

    if colour:
        assert lines[4].endswith("…\x1b[0m  │")
        assert lines[6].endswith("…\x1b[0m  │")
        assert lines[4].count("\x1b[0m") == 2
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
        "WORKING",
        "○ working        Agent-Tools · a-workers",
    ]


def test_active_and_unread_teams_remain_working_rows() -> None:
    """Active roles name both teams, including one with unread messages."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="active"),
        agent("Agent-Tools", "scout", unread=1),
        agent("Custom", "orchestrator", age=30),
        agent("Custom", "scout", status="active", age=20),
        agent("Agent-Tools-old", "scout", status="inactive"),
    ]

    lines = board_content(agents)

    assert lines == ["WORKING", "○ coordinating  Agent-Tools", "◌ checking      Custom"]


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


@pytest.mark.parametrize(
    ("tags", "expected_row"),
    [
        (
            ("Agent-Tools-learner-claude", "Agent-Tools-scout-learn-claude"),
            "▶ learning  Agent-Tools · learner-claude",
        ),
        (
            (
                "Agent-Tools-insights-review-peer-pair-1",
                "Agent-Tools-scout-review-claude",
            ),
            "◎ reviewing  Agent-Tools · insights-review",
        ),
    ],
)
def test_learner_and_review_team_roles_share_one_complete_row(
    tags: tuple[str, str], expected_row: str
) -> None:
    """Learner and review workers share a row with their matching scout."""
    agents = [agent(tags[0], None, status="active"), agent(tags[1], None)]

    assert board_content(agents) == ["WORKING", expected_row]


@pytest.mark.parametrize(
    ("tag", "expected_row"),
    [
        (
            "Agent-Tools-learner-claude",
            "▶ learning  Agent-Tools · learner-claude (partial team)",
        ),
        (
            "Agent-Tools-scout-learn-claude",
            "◌ checking  Agent-Tools · learner-claude (partial team)",
        ),
        (
            "Agent-Tools-insights-review-peer-pair-1",
            "◎ reviewing  Agent-Tools · insights-review (partial team)",
        ),
        (
            "Agent-Tools-scout-review-claude",
            "◌ checking  Agent-Tools · insights-review (partial team)",
        ),
    ],
)
def test_learner_and_review_team_without_matching_role_is_partial(
    tag: str, expected_row: str
) -> None:
    """Each half of a learner or review team stays marked as incomplete."""
    agents = [agent(tag, None, status="active")]

    assert board_content(agents) == ["WORKING", expected_row]


def test_standard_team_with_learner_in_its_label_stays_complete() -> None:
    """A standard role suffix takes precedence over learner text in the label."""
    agents = [
        agent("Agent-Tools-learner-ui", "orchestrator"),
        agent("Agent-Tools-learner-ui", "implementer"),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "● needs you  Agent-Tools · learner-ui",
    ]


def test_repository_name_containing_learner_keeps_standard_team_rule() -> None:
    """A repository name cannot turn an ordinary team into a learner team."""
    agents = [
        agent("e-learner-app-board", "orchestrator", directory="/work/e-learner-app"),
        agent("e-learner-app-board", "implementer", directory="/work/e-learner-app"),
    ]

    assert board_content(agents) == [
        "WAITING ON YOU",
        "● needs you  e-learner-app · board",
    ]


def test_planning_scout_keeps_its_own_non_review_row() -> None:
    """A planning scout does not join an insights review team."""
    agents = [agent("Agent-Tools-scout-peer-claude", None, status="active")]

    assert board_content(agents) == [
        "WORKING",
        "○ working  Agent-Tools · scout-peer-claude (partial team)",
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
        "WAITING ON YOU",
        "\x1b[35m● needs you\x1b[0m  \x1b[97mAgent-Tools · idle\x1b[0m",
        "WORKING",
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
        "\x1b[31m✕ blocked · 2m\x1b[0m  \x1b[97mAgent-Tools · blocked\x1b[0m"
    )
    assert lines[2] == (
        "\x1b[35m● needs you   \x1b[0m  \x1b[97mAgent-Tools · idle\x1b[0m"
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
        "WORKING",
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
