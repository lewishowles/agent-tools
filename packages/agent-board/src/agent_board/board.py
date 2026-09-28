"""Turn an HCOM agent listing into the lines shown on the board."""

import re
from collections import defaultdict
from pathlib import Path

# The board label for each role's work, in priority order: the first role
# with an active agent names what a working team is doing.
ACTIVITY_LABELS = {
    "implementer": "implementing",
    "reviewer": "reviewing",
    "scout": "checking",
    "orchestrator": "coordinating",
}

# The roles the zsh team launcher adds to the end of every agent tag.
ROLES = set(ACTIVITY_LABELS)

# The symbol at the start of each board row, keyed by the row's status.
# A blocked row is looked up without its wait time.
STATUS_SYMBOLS = {
    "needs you": "●",
    "blocked": "✕",
    "implementing": "▶",
    "reviewing": "◎",
    "checking": "◌",
    "coordinating": "○",
    "starting": "◇",
    "working": "○",
}

# The terminal code that brightens the names of teams that need you.
BRIGHT_WHITE_STYLE = "\x1b[97m"

# The terminal code that shows a blocked status in red.
RED_STYLE = "\x1b[31m"

# The terminal code that shows a "needs you" status in purple.
MAGENTA_STYLE = "\x1b[35m"

# The terminal code that dims working teams and the empty state.
DIM_STYLE = "\x1b[2m"

# The terminal code that ends any of the styles above.
RESET_STYLE = "\x1b[0m"


def render_board(agents: list[dict], *, colour: bool = False) -> list[str]:
    """Build the board lines for one `hcom list --json` snapshot.

    The lines start with a blank line. Teams that need you come first:
    blocked teams with their longest wait, then teams whose agents are all
    waiting with nothing unread. Working teams follow after another blank
    line. Statuses line up across both sections, and an empty listing
    shows "No active teams". Stopped (inactive) agents are left out, so a team
    with one agent left shows as a partial team.

    Args:
        agents: The records from `hcom list --json`.
        colour: Whether to colour attention rows and dim working rows.
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
    working = []

    for prefix, members in teams.items():
        label = _team_label(prefix, members)
        statuses = [agent["status"] for agent, _ in members]
        has_orchestrator = any(role == "orchestrator" for _, role in members)
        has_worker = any(role != "orchestrator" for _, role in members)
        partly_running = not (has_orchestrator and has_worker)

        # Only one reason is shown per team; a blocked agent matters most.
        if "blocked" in statuses:
            age = max(
                agent["status_age_seconds"]
                for agent, _ in members
                if agent["status"] == "blocked"
            )
            needs_you.append(((0, -age, label), label, f"blocked · {_format_age(age)}"))
        elif partly_running:
            status = _working_status(members)

            if len(members) == 1:
                label += " (partial team)"

            working.append((label, status))
        elif all(
            agent["status"] == "listening" and agent["unread_count"] == 0
            for agent, _ in members
        ):
            needs_you.append(((1, 0, label), label, "needs you"))
        else:
            working.append((label, _working_status(members)))

    # Blocked teams sort by longest wait; every other team sorts by name.
    needs_you.sort(key=lambda row: row[0])
    working.sort(key=lambda row: row[0])

    lines = [""]

    if not needs_you and not working:
        line = "No active teams"
        lines.append(f"{DIM_STYLE}{line}{RESET_STYLE}" if colour else line)
        return lines

    shown_statuses = [status for _, _, status in needs_you] + [
        status for _, status in working
    ]
    status_width = max(len(status) for status in shown_statuses)

    for _, label, status in needs_you:
        symbol = STATUS_SYMBOLS[status.partition(" · ")[0]]
        padded_status = f"{status:<{status_width}}"

        if colour:
            status_style = RED_STYLE if status.startswith("blocked") else MAGENTA_STYLE
            lines.append(
                f"{status_style}{symbol} {padded_status}{RESET_STYLE}  "
                f"{BRIGHT_WHITE_STYLE}{label}{RESET_STYLE}"
            )
        else:
            lines.append(f"{symbol} {padded_status}  {label}")

    if needs_you and working:
        lines.append("")

    for label, status in working:
        symbol = STATUS_SYMBOLS[status]
        line = f"{symbol} {status:<{status_width}}  {label}"
        lines.append(f"{DIM_STYLE}{line}{RESET_STYLE}" if colour else line)

    return lines


def _working_status(members: list[tuple[dict, str | None]]) -> str:
    """Name the work of the highest-priority active role in a team.

    A team with no active member in a known role is starting if anyone is
    launching, or working otherwise.
    """
    for role, label in ACTIVITY_LABELS.items():
        if any(
            agent["status"] == "active" and member_role == role
            for agent, member_role in members
        ):
            return label

    if any(agent["status"] == "launching" for agent, _ in members):
        return "starting"

    return "working"


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
