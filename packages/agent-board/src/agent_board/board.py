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
    "stale": "●",
    "stuck": "●",
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

# The terminal code for the amber title and clock, and for the border while
# no team is waiting on you.
AMBER_STYLE = "\x1b[38;5;214m"

# The terminal code that dims working teams and the empty state.
DIM_STYLE = "\x1b[2m"

# The terminal code that ends any of the styles above.
RESET_STYLE = "\x1b[0m"

# The pattern that matches ANSI colour codes, which take up no screen space.
ANSI_STYLE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

# The narrowest terminal that still gets the frame. Narrower terminals get
# plain rows, because the title and clock alone need 29 columns.
MIN_FRAME_WIDTH = 30

# The widest the frame gets. Wider terminals leave the extra columns empty to
# the right, so the clock and counts stay close to the rows they describe.
MAX_FRAME_WIDTH = 80

# How long a waiting agent can leave messages unread before its team shows as
# stuck. Messages normally wake an agent within a refresh or two, so a longer
# wait means delivery has stalled and the team needs you.
STUCK_AFTER_SECONDS = 60

# A working team needs attention when none of its members has had an HCOM
# event for this long.
QUIET_AFTER_SECONDS = 300


def active_team_members(agents: list[dict]) -> list[dict]:
    """Return members of teams that currently have an active agent."""
    return [
        agent
        for members in _group_teams(agents).values()
        if any(member["status"] == "active" for member, _ in members)
        for agent, _ in members
    ]


def quiet_team_members(
    agents: list[dict], last_activity: dict[str, float], now: float
) -> dict[str, str]:
    """Find the active member to blame for each team that has gone quiet.

    A team is quiet when it has an active member and none of its members has
    had an event for QUIET_AFTER_SECONDS. The result maps each quiet team,
    keyed by the tag its members share without their roles (or the agent's
    name when it has no tag), to the name of its active member that has been
    silent longest, whose terminal the board reads for a reason. A member with
    no known event time keeps its team working until that time is known.
    """
    quiet = {}

    for key, members in _group_teams(agents).items():
        active = [agent for agent, _ in members if agent["status"] == "active"]

        if not active or any(
            agent["name"] not in last_activity
            or now - last_activity[agent["name"]] < QUIET_AFTER_SECONDS
            for agent, _ in members
        ):
            continue

        quiet[key] = min(active, key=lambda agent: last_activity[agent["name"]])["name"]

    return quiet


def render_board(
    agents: list[dict],
    *,
    width: int,
    current_time: str,
    colour: bool = False,
    unread_since: dict[str, float] | None = None,
    last_activity: dict[str, float] | None = None,
    quiet_members: dict[str, str] | None = None,
    quiet_reasons: dict[str, str | None] | None = None,
    now: float = 0,
    phase_since: dict[str, tuple[str, float, bool]] | None = None,
) -> list[str]:
    """Build the framed board lines for one `hcom list --json` snapshot.

    The lines start with a blank line, then the frame. Teams that need you come
    first under WAITING ON YOU: blocked teams with their longest wait, then
    stale teams, stuck teams and teams whose agents are all waiting with
    nothing unread.
    Working teams follow under WORKING, after a blank line when both groups
    show. A heading is left out when its group is empty.
    Below 30 columns, plain rows replace the frame and headings, with a blank
    line between the groups.
    Statuses line up across both groups, and an empty listing shows
    "No active teams". Stopped (inactive) agents are left out, so a team with
    one agent left shows as a partial team.

    Args:
        agents: The records from `hcom list --json`.
        width: The terminal's width for this refresh.
        current_time: The local time for the top edge, formatted as HH:MM:SS.
        colour: Whether to colour the frame and rows and dim the headings,
            working rows and team names after the repository.
        unread_since: The first time each listening agent was seen with unread
            messages, keyed by agent name.
        last_activity: The latest HCOM event time for each active team's
            members, keyed by agent name.
        quiet_members: The active member to blame for each quiet team, keyed
            by team. Its terminal gives the stuck reason.
        quiet_reasons: Known terminal failure reasons, keyed by active agent name.
        now: The monotonic time for this refresh, in seconds.
        phase_since: The phase timers from the previous refresh, keyed by team.
            Each entry holds the phase, its known start time, and whether the
            team was already in that phase when the board first saw it. Stuck
            phases start at the event or unread time that made the team quiet.
            The board updates this in place and removes teams that have gone.
            When omitted, rows show no timer except blocked HCOM wait times.
    """
    teams = _group_teams(agents)
    unread_since = unread_since or {}
    last_activity = last_activity or {}

    quiet_members = quiet_members or {}
    quiet_reasons = quiet_reasons or {}

    needs_you = []
    working = []
    stuck_since = {}

    for prefix, members in teams.items():
        repository, label_rest = _team_label(prefix, members)
        label = repository + label_rest
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
            needs_you.append(
                (
                    (0, -age, label),
                    repository,
                    label_rest,
                    f"blocked · {_format_age(age)}",
                    prefix,
                )
            )
        # hcom can move a worker onto a background Codex session that then
        # stops updating, so the worker stays active and hcom holds messages for it.
        # That session no longer matches the worker's transcript.
        elif any(
            agent["status"] == "active"
            and (session_id := agent.get("session_id"))
            and (transcript_path := agent.get("transcript_path"))
            and session_id not in transcript_path
            for agent, _ in members
        ):
            needs_you.append(((1, 0, label), repository, label_rest, "stale", prefix))
        elif (quiet_member := quiet_members.get(prefix)) is not None:
            reason = quiet_reasons.get(quiet_member)
            status = f"stuck · {reason}" if reason else "stuck"
            if all(agent["name"] in last_activity for agent, _ in members):
                stuck_since[prefix] = max(
                    last_activity[agent["name"]] for agent, _ in members
                )
            needs_you.append(((2, 0, label), repository, label_rest, status, prefix))
        elif not any(status in {"active", "launching"} for status in statuses) and (
            old_unread_times := [
                unread_since[agent["name"]]
                for agent, _ in members
                if agent["status"] == "listening"
                and agent["unread_count"] > 0
                and agent["name"] in unread_since
                and now - unread_since[agent["name"]] > STUCK_AFTER_SECONDS
            ]
        ):
            stuck_since[prefix] = min(old_unread_times)
            needs_you.append(((2, 0, label), repository, label_rest, "stuck", prefix))
        elif partly_running:
            status = _working_status(members)

            if len(members) == 1:
                label_rest += " (partial team)"

            working.append((label, repository, label_rest, status, prefix))
        elif all(
            agent["status"] == "listening" and agent["unread_count"] == 0
            for agent, _ in members
        ):
            needs_you.append(
                ((2, 0, label), repository, label_rest, "needs you", prefix)
            )
        else:
            working.append(
                (
                    label,
                    repository,
                    label_rest,
                    _working_status(members),
                    prefix,
                )
            )

    if phase_since is not None:
        for key in list(phase_since):
            if key not in teams:
                del phase_since[key]

    needs_you = [
        (
            order,
            repository,
            label_rest,
            _phase_status(status, key, phase_since, now, stuck_since.get(key)),
        )
        for order, repository, label_rest, status, key in needs_you
    ]
    working = [
        (label, repository, label_rest, _phase_status(status, key, phase_since, now))
        for label, repository, label_rest, status, key in working
    ]

    # Blocked teams come first, longest wait first. Stale teams follow, then
    # stuck and waiting teams, each group by name. Working teams sort by name.
    needs_you.sort(key=lambda row: row[0])
    working.sort(key=lambda row: row[0])

    # The border follows the most urgent team and is amber when none is waiting.
    if any(status.startswith("blocked") for _, _, _, status in needs_you):
        frame_style = RED_STYLE
    elif needs_you:
        frame_style = MAGENTA_STYLE
    else:
        frame_style = AMBER_STYLE

    # Narrow terminals get plain rows without the frame or headings.
    framed = width >= MIN_FRAME_WIDTH
    lines = []

    if not needs_you and not working:
        line = "No active teams"
        lines.append(f"{DIM_STYLE}{line}{RESET_STYLE}" if colour else line)
        if not framed:
            return _plain_lines(lines, width)

        return _frame_lines(
            lines,
            width,
            current_time,
            "0 waiting on you · 0 working",
            frame_style=frame_style if colour else None,
        )

    shown_statuses = [status for _, _, _, status in needs_you] + [
        status for _, _, _, status in working
    ]
    status_width = max(len(status) for status in shown_statuses)

    if needs_you and framed:
        heading = "WAITING ON YOU"
        lines.append(f"{DIM_STYLE}{heading}{RESET_STYLE}" if colour else heading)

    for _, repository, label_rest, status in needs_you:
        symbol = STATUS_SYMBOLS[status.partition(" · ")[0]]
        padded_status = f"{status:<{status_width}}"

        if colour:
            status_style = RED_STYLE if status.startswith("blocked") else MAGENTA_STYLE
            styled_rest = f"{DIM_STYLE}{label_rest}{RESET_STYLE}" if label_rest else ""
            lines.append(
                f"{status_style}{symbol} {padded_status}{RESET_STYLE}  "
                f"{BRIGHT_WHITE_STYLE}{repository}{RESET_STYLE}"
                f"{styled_rest}"
            )
        else:
            lines.append(f"{symbol} {padded_status}  {repository}{label_rest}")

    if working and framed:
        if needs_you:
            lines.append("")

        heading = "WORKING"
        lines.append(f"{DIM_STYLE}{heading}{RESET_STYLE}" if colour else heading)
    elif working and needs_you:
        # Without headings, a blank line keeps the two groups apart.
        lines.append("")

    for _, repository, label_rest, status in working:
        symbol = STATUS_SYMBOLS[status.partition(" · ")[0]]
        line = f"{symbol} {status:<{status_width}}  {repository}{label_rest}"
        lines.append(f"{DIM_STYLE}{line}{RESET_STYLE}" if colour else line)

    if not framed:
        return _plain_lines(lines, width)

    bottom_text = f"{len(needs_you)} waiting on you · {len(working)} working"
    return _frame_lines(
        lines,
        width,
        current_time,
        bottom_text,
        frame_style=frame_style if colour else None,
    )


def render_error(
    message: str, *, width: int, current_time: str, colour: bool = False
) -> list[str]:
    """Show an hcom failure inside the frame, or as plain lines below 30 columns.

    Args:
        message: The error to show inside the frame.
        width: The terminal's width for this refresh.
        current_time: The local time for the top edge, formatted as HH:MM:SS.
        colour: Whether to colour the frame amber.
    """
    lines = message.splitlines()

    if width < MIN_FRAME_WIDTH:
        return _plain_lines(lines, width)

    return _frame_lines(
        lines,
        width,
        current_time,
        None,
        frame_style=AMBER_STYLE if colour else None,
    )


def _plain_lines(lines: list[str], width: int) -> list[str]:
    """Show the board's lines without a frame, each shortened to the terminal width.

    The leading empty line matches the gap above the frame on wider terminals.
    """
    return ["", *(_shorten_visible(line, width) for line in lines)]


def _frame_lines(
    lines: list[str],
    width: int,
    current_time: str,
    bottom_text: str | None,
    *,
    frame_style: str | None = None,
) -> list[str]:
    """Draw the rounded frame around the board's lines.

    A blank row sits above and below the content. Content lines sit two spaces
    in from each side, and the right padding ignores colour codes. Content and
    bottom-edge text that would not fit end in "…", so the right edge always
    lines up.

    Args:
        lines: The content to show inside the frame.
        width: The terminal's width for this refresh. The frame is never wider
            than MAX_FRAME_WIDTH.
        current_time: The local time for the top edge, formatted as HH:MM:SS.
        bottom_text: The text set into the bottom edge, or None for a plain edge.
        frame_style: The terminal colour for the border and the bottom-edge
            text, or None to draw the frame without colour.
    """
    width = min(width, MAX_FRAME_WIDTH)
    top_start = "╭─ ✻ agent board "
    top_end = f" {current_time} ─╮"
    top_dashes = "─" * max(0, width - len(top_start) - len(top_end))
    top = top_start + top_dashes + top_end

    if frame_style:
        top = (
            f"{frame_style}╭─ {RESET_STYLE}{AMBER_STYLE}✻ agent board{RESET_STYLE}"
            f"{frame_style} {top_dashes} {RESET_STYLE}{AMBER_STYLE}{current_time}"
            f"{RESET_STYLE}{frame_style} ─╮{RESET_STYLE}"
        )

    # The corners, the dashes beside the text and the spaces around it use six
    # columns.
    bottom_start = (
        f"╰─ {_shorten_visible(bottom_text, width - 6)} "
        if bottom_text is not None
        else "╰"
    )
    bottom_end = "─╯" if bottom_text is not None else "╯"
    bottom = (
        bottom_start
        + "─" * max(0, width - len(bottom_start) - len(bottom_end))
        + bottom_end
    )

    if frame_style:
        bottom = f"{frame_style}{bottom}{RESET_STYLE}"

    # The borders and two spaces on each side use six columns.
    content_width = width - 6
    blank_row = f"│{' ' * (width - 2)}│"

    if frame_style:
        blank_row = (
            f"{frame_style}│{RESET_STYLE}{' ' * (width - 2)}{frame_style}│{RESET_STYLE}"
        )

    framed_rows = []

    for line in lines:
        short_line = _shorten_visible(line, content_width)
        padding = " " * (content_width - _visible_width(short_line))

        if frame_style:
            framed_rows.append(
                f"{frame_style}│{RESET_STYLE}  {short_line}{padding}  "
                f"{frame_style}│{RESET_STYLE}"
            )
        else:
            framed_rows.append(f"│  {short_line}{padding}  │")

    return ["", top, blank_row, *framed_rows, blank_row, bottom]


def _visible_width(line: str) -> int:
    """Count the characters that occupy a row, leaving out ANSI colour codes."""
    return len(ANSI_STYLE_PATTERN.sub("", line))


def _shorten_visible(line: str, width: int) -> str:
    """Shorten a line to fit a number of columns, ending it with "…" when cut.

    Colour codes take no columns and are never split. A shortened line that
    contains colour ends with a reset, so the colour stops at the "…".
    """
    if _visible_width(line) <= width:
        return line

    if width <= 0:
        return ""

    # One column is kept free for the "…".
    limit = width - 1
    parts = []
    visible = 0
    start = 0

    for match in ANSI_STYLE_PATTERN.finditer(line):
        segment = line[start : match.start()]
        remaining = limit - visible

        if len(segment) >= remaining:
            parts.append(segment[:remaining])
            break

        parts.extend((segment, match.group()))
        visible += len(segment)
        start = match.end()
    else:
        parts.append(line[start : start + limit - visible])

    shortened = "".join(parts) + "…"
    return (
        shortened + RESET_STYLE if ANSI_STYLE_PATTERN.search(shortened) else shortened
    )


def _group_teams(
    agents: list[dict],
) -> dict[str, list[tuple[dict, str | None]]]:
    """Group live agents by their team tag for polling and display."""
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

    return teams


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
    """Split an agent tag into the team it belongs to and its role.

    A tag that does not end in a known role is its own team, with no role.
    """
    prefix, separator, role = tag.rpartition("-")

    if separator and role in ROLES:
        return prefix, role

    return tag, None


def _team_label(prefix: str, members: list[tuple[dict, str | None]]) -> tuple[str, str]:
    """Split a team's name into its repository and the rest, such as " · label".

    The repository is the longest folder name, from any member's working
    directory or its parents, that the tag prefix starts with. Agents often
    work in a subfolder, and a short subfolder name must not win over the
    repository. A prefix with no matching folder is shown as it is, so the
    agent is still listed. The rest is empty when the team is named after its
    repository alone; the board dims it on rows that are waiting on you.
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
        return repository, ""

    if repository:
        return repository, f" · {normalised_prefix[len(repository) + 1 :]}"

    return prefix, ""


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


def _phase_status(
    status: str,
    key: str,
    phase_since: dict[str, tuple[str, float, bool]] | None,
    now: float,
    stuck_start: float | None = None,
) -> str:
    """Add how long the team has been in its current phase to its status.

    The phase is the status text before any ` · ` detail, so `stuck · model at
    capacity` and `stuck` are the same phase. A team the board sees for the
    first time gets a `+` after its time, because it may have been in that
    phase for longer than the board has been watching. A stuck team's time
    counts from when it went quiet. A blocked status is returned unchanged,
    because it already shows HCOM's exact wait time.

    Args:
        status: The status text the row would otherwise show.
        key: The tag the team's members share, without their roles, or the
            agent's name when it has no tag.
        phase_since: The phase timers from the previous refresh. A new or
            changed phase is recorded here in place. When None, the status is
            returned unchanged.
        now: The monotonic time for this refresh, in seconds.
        stuck_start: When the team went quiet: its last HCOM event, or when
            the board first saw its oldest unread messages. Used only when
            the team enters the stuck phase; without it the timer starts now.

    Returns:
        The status followed by its phase duration, such as `checking · 30s`,
        or the unchanged blocked status with HCOM's wait time.
    """
    if phase_since is None:
        return status

    phase = status.partition(" · ")[0]
    previous = phase_since.get(key)

    if previous is None or previous[0] != phase:
        phase_started = (
            stuck_start if phase == "stuck" and stuck_start is not None else now
        )
        estimated = previous is None
        phase_since[key] = (phase, phase_started, estimated)
    else:
        _, phase_started, estimated = previous

    if phase == "blocked":
        return status

    suffix = "+" if estimated else ""
    return f"{status} · {_format_age(now - phase_started)}{suffix}"
