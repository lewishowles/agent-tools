"""Turn an HCOM agent listing into the lines shown on the board."""

import re
from collections import defaultdict
from pathlib import Path

# The roles the zsh team launcher adds to the end of every agent tag.
ROLES = {"orchestrator", "reviewer", "implementer", "scout"}

# The terminal code that dims the quiet line and the rows for lone agents.
DIM_STYLE = "\x1b[2m"

# The terminal code that ends dimmed text.
RESET_STYLE = "\x1b[0m"


def render_board(agents: list[dict], *, colour: bool = False) -> list[str]:
    """Build the board lines for one `hcom list --json` snapshot.

    Blocked teams come first with their longest wait. Other teams needing the
    human are sorted by name without a time, with lone agents after them.
    Every other team is named on one quiet line at the end. Stopped (inactive)
    agents are left out, so a team that has lost its orchestrator shows as
    partly running.

    Args:
        agents: The records from `hcom list --json`.
        colour: Whether to dim the quiet line and lone-agent rows.
    """
    teams = defaultdict(list)

    for agent in agents:
        if agent["status"] == "inactive":
            continue

        tag = agent.get("tag")

        if tag:
            prefix, role = _split_tag(tag)
        else:
            # An untagged agent forms its own team so it stays on the board.
            prefix, role = agent["name"], None

        teams[prefix].append((agent, role))

    needs_you = []
    quiet = []

    for prefix, members in teams.items():
        label = _team_label(prefix, members)
        statuses = [agent["status"] for agent, _ in members]
        has_orchestrator = any(role == "orchestrator" for _, role in members)
        has_worker = any(role != "orchestrator" for _, role in members)
        partly_running = not (has_orchestrator and has_worker)

        # Only one reason is shown per team; a blocked agent matters most.
        if "blocked" in statuses:
            reason = "blocked"
            age = max(
                agent["status_age_seconds"]
                for agent, _ in members
                if agent["status"] == "blocked"
            )
        elif partly_running:
            reason = "partly running"
            age = None
        elif all(
            agent["status"] == "listening" and agent["unread_count"] == 0
            for agent, _ in members
        ):
            reason = "idle"
            age = None
        else:
            quiet.append(label)
            continue

        # Blocked teams sort by wait; other teams sort by name, with lone agents last.
        lone_agent = len(members) == 1 and reason == "partly running"

        if reason == "blocked":
            sort_key = (0, -age, label)
        elif lone_agent:
            sort_key = (2, 0, label)
        else:
            sort_key = (1, 0, label)

        needs_you.append((sort_key, label, reason, age, lone_agent))

    needs_you.sort(key=lambda row: row[0])
    lines = ["Needs you"]

    for _, label, reason, age, lone_agent in needs_you:
        line = f"{label}: {reason}"

        if age is not None:
            line += f" · {_format_age(age)}"

        if lone_agent and colour:
            line = f"{DIM_STYLE}{line}{RESET_STYLE}"

        lines.append(line)

    if len(lines) == 1:
        lines.append("None")

    quiet_line = f"Quiet: {', '.join(sorted(quiet))}" if quiet else "Quiet: none"

    if colour:
        quiet_line = f"{DIM_STYLE}{quiet_line}{RESET_STYLE}"

    lines.append(quiet_line)

    return lines


def _normalise(value: str) -> str:
    """Normalise a folder name or tag prefix the way the zsh team launcher builds tags.

    Each run of characters other than letters and digits becomes one hyphen,
    and trailing hyphens are dropped.
    """
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).rstrip("-")


def _split_tag(tag: str) -> tuple[str, str | None]:
    """Split a tag into its team prefix and role.

    A tag that does not end in a known role comes back whole, with no role.
    """
    prefix, separator, role = tag.rpartition("-")

    if separator and role in ROLES:
        return prefix, role

    return tag, None


def _team_label(prefix: str, members: list[tuple[dict, str | None]]) -> str:
    """Name a team as `repo` or `repo · label`.

    The repository is the longest folder name, from any member's working
    directory or its parents, that the tag prefix starts with. Agents often
    work in a subfolder, and a short subfolder name must not win over the
    repository. A prefix with no matching folder is shown as it is, so the
    agent is still listed.
    """
    normalised_prefix = _normalise(prefix)
    repository = ""

    for agent, _ in members:
        directory = Path(agent["directory"])

        for candidate in (directory, *directory.parents):
            folder_name = _normalise(candidate.name)

            matches_prefix = folder_name == normalised_prefix or (
                folder_name and normalised_prefix.startswith(f"{folder_name}-")
            )

            if matches_prefix and len(folder_name) > len(repository):
                repository = folder_name

    if repository == normalised_prefix:
        return repository

    if repository:
        return f"{repository} · {normalised_prefix[len(repository) + 1 :]}"

    return prefix


def _format_age(seconds: float) -> str:
    """Show how long ago a status changed in its largest whole unit, such as `5m`."""
    seconds = max(0, int(seconds))

    if seconds >= 86400:
        return f"{seconds // 86400}d"

    if seconds >= 3600:
        return f"{seconds // 3600}h"

    if seconds >= 60:
        return f"{seconds // 60}m"

    return f"{seconds}s"
