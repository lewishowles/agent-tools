"""Check board decisions against representative HCOM listings."""

import pytest
from agent_board.board import render_board


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


def test_attention_and_working_groups_use_priority_and_align_labels() -> None:
    """Blocked teams come first, and every row shares a colon column."""
    agents = [
        agent("Agent-Tools-partial", "orchestrator", age=70),
        agent("Agent-Tools-idle", "orchestrator", age=90),
        agent("Agent-Tools-idle", "scout", age=80),
        agent("Agent-Tools-blocked", "orchestrator", status="blocked", age=100),
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-blocked", "reviewer", status="active", age=10),
    ]

    lines = render_board(agents)

    assert lines == [
        "",
        "· Agent-Tools · blocked : blocked · 2m",
        "· Agent-Tools · idle    : needs you",
        "",
        "· Agent-Tools · partial : working (partial team)",
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

    assert render_board(agents) == [
        "",
        "· Agent-Tools · z-blocked : blocked · 2m",
        "· Agent-Tools · a-blocked : blocked · 30s",
        "· Agent-Tools · z-idle    : needs you",
        "",
        "· Agent-Tools · a-workers : working",
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

    lines = render_board(agents)

    assert lines == ["", "· Agent-Tools : coordinating", "· Custom      : checking"]


@pytest.mark.parametrize(
    ("role", "label", "partner_role"),
    [
        ("implementer", "implementing", "orchestrator"),
        ("reviewer", "reviewing", "orchestrator"),
        ("scout", "checking", "orchestrator"),
        ("orchestrator", "coordinating", "scout"),
    ],
)
def test_active_role_names_working_team(
    role: str, label: str, partner_role: str
) -> None:
    """Each recognised active role gives the team its activity label."""
    agents = [
        agent("Agent-Tools", role, status="active"),
        agent("Agent-Tools", partner_role, status="listening"),
    ]

    assert render_board(agents)[1] == f"· Agent-Tools : {label}"


@pytest.mark.parametrize(
    ("roles", "label"),
    [
        (["orchestrator", "scout"], "checking"),
        (["scout", "reviewer"], "reviewing"),
        (["reviewer", "implementer"], "implementing"),
    ],
)
def test_active_role_priority_names_working_team(roles: list[str], label: str) -> None:
    """The highest-priority active role names a team with concurrent work."""
    agents = [agent("Agent-Tools", role, status="active") for role in roles]

    assert render_board(agents)[1] == f"· Agent-Tools : {label}"


def test_launching_team_is_starting_without_active_members() -> None:
    """Launching takes precedence over the working fallback."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="launching"),
        agent("Agent-Tools", "scout", status="listening"),
    ]

    assert render_board(agents)[1] == "· Agent-Tools : starting"


def test_active_role_takes_priority_over_launching_member() -> None:
    """An active role names the team while another member launches."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="launching"),
        agent("Agent-Tools", "scout", status="active"),
    ]

    assert render_board(agents)[1] == "· Agent-Tools : checking"


def test_unrecognised_active_role_keeps_working_fallback() -> None:
    """An active agent with no known role cannot choose the team label."""
    untagged_agent = agent("Agent-Tools", None, status="active")
    untagged_agent["name"] = "Agent-Tools"
    untagged_agent["tag"] = None
    agents = [
        agent("Agent-Tools", "orchestrator"),
        untagged_agent,
    ]

    assert render_board(agents)[1] == "· Agent-Tools : working"


def test_active_partial_team_keeps_suffix() -> None:
    """A lone active agent shows its work and remains marked as partial."""
    agents = [agent("Agent-Tools", "implementer", status="active")]

    assert render_board(agents)[1] == "· Agent-Tools : implementing (partial team)"


def test_single_live_agent_joins_dimmed_working_group() -> None:
    """An incomplete one-agent team is marked as partial and dimmed."""
    agents = [
        agent("Agent-Tools-alone", "scout", age=3600),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = render_board(agents, colour=True)

    assert lines == [
        "",
        "· \x1b[97mAgent-Tools · idle \x1b[0m : \x1b[35mneeds you\x1b[0m",
        "",
        "\x1b[2m· Agent-Tools · alone : working (partial team)\x1b[0m",
    ]


def test_directory_normalisation_without_idle_age() -> None:
    """Folder names become tag-style labels while waiting rows omit ages."""
    agents = [
        agent("Lew-Timer", "orchestrator", directory="/work/Lew Timer!", age=119),
        agent("Lew-Timer", "reviewer", directory="/work/Lew Timer!", age=40),
    ]

    assert render_board(agents)[1] == "· Lew-Timer : needs you"


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

    assert render_board(agents) == [
        "",
        "· Agent-Tools · board : needs you",
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

    assert render_board(agents)[1] == "· Agent-Tools · board : needs you"


def test_workers_without_orchestrator_are_working() -> None:
    """A team with two workers is working even without an orchestrator."""
    agents = [
        agent("Agent-Tools-workers", "scout", age=100),
        agent("Agent-Tools-workers", "reviewer", age=80),
    ]

    assert render_board(agents, colour=True)[1] == (
        "\x1b[2m· Agent-Tools · workers : working\x1b[0m"
    )


def test_distinct_prefixes_with_the_same_label_are_both_shown() -> None:
    """Teams with equal display labels remain separate working rows."""
    agents = [
        agent("Agent-Tools-a_b", "scout"),
        agent("Agent-Tools-a_b", "reviewer"),
        agent("Agent-Tools-a--b", "scout"),
        agent("Agent-Tools-a--b", "reviewer"),
    ]

    assert render_board(agents) == [
        "",
        "· Agent-Tools · a-b : working",
        "· Agent-Tools · a-b : working",
    ]


def test_lone_blocked_agent_keeps_age_order_and_red_status() -> None:
    """A blocked lone agent keeps its wait and the attention colours."""
    agents = [
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = render_board(agents, colour=True)

    assert lines[1] == (
        "· \x1b[97mAgent-Tools · blocked\x1b[0m : \x1b[31mblocked · 2m\x1b[0m"
    )
    assert lines[2] == (
        "· \x1b[97mAgent-Tools · idle   \x1b[0m : \x1b[35mneeds you\x1b[0m"
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

    assert render_board(agents, colour=True) == [
        "",
        "\x1b[2m· Agent-Tools · active  : coordinating\x1b[0m",
        "\x1b[2m· Agent-Tools · unread  : working\x1b[0m",
        "\x1b[2m· Agent-Tools · workers : working\x1b[0m",
    ]


def test_empty_groups_add_no_separator() -> None:
    """The board starts blank without adding a gap for a missing group."""
    waiting = [agent("Agent-Tools", "orchestrator"), agent("Agent-Tools", "scout")]

    assert render_board(waiting) == ["", "· Agent-Tools : needs you"]
    assert render_board([agent("Agent-Tools", "scout")]) == [
        "",
        "· Agent-Tools : working (partial team)",
    ]


def test_no_active_teams_shows_one_dimmed_message() -> None:
    """An empty or inactive listing gives a single message after the blank line."""
    assert render_board([]) == ["", "No active teams"]
    assert render_board([], colour=True) == ["", "\x1b[2mNo active teams\x1b[0m"]
    assert render_board([agent("Agent-Tools", "scout", status="inactive")]) == [
        "",
        "No active teams",
    ]


def test_unfamiliar_tag_without_role_keeps_raw_prefix() -> None:
    """An unfamiliar tag remains on the board as its own team."""
    entry = agent("CustomTag", None, status="active", age=30)

    assert render_board([entry])[1] == "· CustomTag : working (partial team)"


def test_missing_tag_uses_agent_name_as_team_prefix() -> None:
    """An agent without a tag remains visible under its own name."""
    for missing_tag in (None, ""):
        entry = agent("Agent-Tools-standalone-lamo", None, age=30)
        entry["name"] = "Agent-Tools-standalone-lamo"
        entry["tag"] = missing_tag

        assert render_board([entry])[1] == (
            "· Agent-Tools · standalone-lamo : working (partial team)"
        )

    entry = agent("Agent-Tools-standalone-lamo", None, age=30)
    entry["name"] = "Agent-Tools-standalone-lamo"
    del entry["tag"]

    assert render_board([entry])[1] == (
        "· Agent-Tools · standalone-lamo : working (partial team)"
    )
