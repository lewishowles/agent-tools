"""Check board decisions against representative HCOM listings."""

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


def test_needs_you_reasons_use_priority_and_correct_ages() -> None:
    """Blocked teams show an age before the untimed rows sorted by name."""
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
        "Needs you",
        "Agent-Tools · blocked: blocked · 2m",
        "Agent-Tools · idle: idle",
        "Agent-Tools · partial: partly running",
        "Quiet: none",
    ]


def test_blocked_waits_sort_before_untimed_team_names() -> None:
    """Blocked waits rank by duration while other teams rank by name."""
    agents = [
        agent("Agent-Tools-z-idle", "orchestrator", age=500),
        agent("Agent-Tools-z-idle", "scout", age=500),
        agent("Agent-Tools-a-workers", "scout", age=10),
        agent("Agent-Tools-a-workers", "reviewer", age=10),
        agent("Agent-Tools-a-blocked", "scout", status="blocked", age=30),
        agent("Agent-Tools-z-blocked", "scout", status="blocked", age=120),
    ]

    assert render_board(agents)[1:-1] == [
        "Agent-Tools · z-blocked: blocked · 2m",
        "Agent-Tools · a-blocked: blocked · 30s",
        "Agent-Tools · a-workers: partly running",
        "Agent-Tools · z-idle: idle",
    ]


def test_quiet_teams_and_unfamiliar_tags_remain_visible() -> None:
    """Active teams stay quiet and unfamiliar prefixes keep their raw text."""
    agents = [
        agent("Agent-Tools", "orchestrator", status="active"),
        agent("Agent-Tools", "scout", unread=1),
        agent("Custom", "orchestrator", age=30),
        agent("Custom", "scout", status="active", age=20),
        agent("Agent-Tools-old", "scout", status="inactive"),
    ]

    lines = render_board(agents)

    assert lines == ["Needs you", "None", "Quiet: Agent-Tools, Custom"]


def test_single_live_agent_sorts_last_and_is_dimmed() -> None:
    """An incomplete one-agent team follows every other needs-you row."""
    agents = [
        agent("Agent-Tools-alone", "scout", age=3600),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = render_board(agents, colour=True)

    assert lines[1] == "Agent-Tools · idle: idle"
    assert lines[2] == "\x1b[2mAgent-Tools · alone: partly running\x1b[0m"
    assert lines[3] == "\x1b[2mQuiet: none\x1b[0m"


def test_directory_normalisation_without_idle_age() -> None:
    """Folder names become tag-style labels while idle rows omit ages."""
    agents = [
        agent("Lew-Timer", "orchestrator", directory="/work/Lew Timer!", age=119),
        agent("Lew-Timer", "reviewer", directory="/work/Lew Timer!", age=40),
    ]

    assert render_board(agents)[1] == "Lew-Timer: idle"


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
        "Needs you",
        "Agent-Tools · board: idle",
        "Quiet: none",
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

    assert render_board(agents)[1] == "Agent-Tools · board: idle"


def test_workers_without_orchestrator_are_partly_running_at_full_strength() -> None:
    """A team with two workers needs attention without lone-agent dimming."""
    agents = [
        agent("Agent-Tools-workers", "scout", age=100),
        agent("Agent-Tools-workers", "reviewer", age=80),
    ]

    assert render_board(agents, colour=True)[1] == (
        "Agent-Tools · workers: partly running"
    )


def test_distinct_prefixes_with_the_same_label_are_both_shown() -> None:
    """Teams with equal display labels remain separate needs-you rows."""
    agents = [
        agent("Agent-Tools-a_b", "scout"),
        agent("Agent-Tools-a_b", "reviewer"),
        agent("Agent-Tools-a--b", "scout"),
        agent("Agent-Tools-a--b", "reviewer"),
    ]

    assert render_board(agents) == [
        "Needs you",
        "Agent-Tools · a-b: partly running",
        "Agent-Tools · a-b: partly running",
        "Quiet: none",
    ]


def test_lone_blocked_agent_keeps_age_order_and_full_strength() -> None:
    """A blocked lone agent is not treated like a newly started team."""
    agents = [
        agent("Agent-Tools-blocked", "scout", status="blocked", age=120),
        agent("Agent-Tools-idle", "orchestrator", age=60),
        agent("Agent-Tools-idle", "scout", age=60),
    ]

    lines = render_board(agents, colour=True)

    assert lines[1] == "Agent-Tools · blocked: blocked · 2m"
    assert lines[2] == "Agent-Tools · idle: idle"


def test_unfamiliar_tag_without_role_keeps_raw_prefix() -> None:
    """An unfamiliar tag remains on the board as its own team."""
    entry = agent("CustomTag", None, age=30)

    assert render_board([entry])[1] == "CustomTag: partly running"


def test_missing_tag_uses_agent_name_as_team_prefix() -> None:
    """An agent without a tag remains visible under its own name."""
    for missing_tag in (None, ""):
        entry = agent("Agent-Tools-standalone-lamo", None, age=30)
        entry["name"] = "Agent-Tools-standalone-lamo"
        entry["tag"] = missing_tag

        assert render_board([entry])[1] == (
            "Agent-Tools · standalone-lamo: partly running"
        )

    entry = agent("Agent-Tools-standalone-lamo", None, age=30)
    entry["name"] = "Agent-Tools-standalone-lamo"
    del entry["tag"]

    assert render_board([entry])[1] == ("Agent-Tools · standalone-lamo: partly running")
