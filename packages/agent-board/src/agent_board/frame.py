"""Draw board frames and fit coloured text to terminal columns."""

import re
from itertools import zip_longest

# The terminal code for the amber title and clock, and for the border while
# no team is waiting on you.
AMBER_STYLE = "\x1b[38;5;214m"

# The terminal code that ends any colour style.
RESET_STYLE = "\x1b[0m"

# The pattern that matches ANSI colour codes, which take up no screen space.
ANSI_STYLE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

# The widest the teams frame gets, so its clock and counts stay close to the
# rows they describe. The quota panel shares this limit, and on wide terminals
# it fills the space to the right of the frame.
MAX_FRAME_WIDTH = 80

# The blank columns between the teams frame and the quota panel when they sit
# side by side.
_SIDE_PANEL_GAP = 2


def _join_columns(board_lines: list[str], panel_lines: list[str]) -> list[str]:
    """Show the quota panel to the right of the teams frame.

    The board's blank first line stays on its own, so the top edges of the
    teams frame and the quota panel sit on the same row. When one column runs
    out, the other carries on alone, and quota rows below the frame keep the
    panel's column.
    """
    top_line, *board_rows = board_lines
    rows = []

    for board_line, panel_line in zip_longest(board_rows, panel_lines, fillvalue=""):
        if not panel_line:
            rows.append(board_line)
            continue

        padding = " " * (MAX_FRAME_WIDTH - _visible_width(board_line))
        rows.append(f"{board_line}{padding}{' ' * _SIDE_PANEL_GAP}{panel_line}")

    return [top_line, *rows]


def _plain_lines(lines: list[str], width: int) -> list[str]:
    """Show the board's lines without a frame, each shortened to the terminal width.

    The leading empty line matches the gap above the frame on wider terminals.
    """
    return ["", *(shorten_visible(line, width) for line in lines)]


def frame_lines(
    lines: list[str],
    width: int,
    top_right_text: str,
    bottom_text: str | None,
    *,
    frame_style: str | None = None,
    title: str = "✻ agent board",
) -> list[str]:
    """Draw a rounded frame around the board or quota panel's lines.

    A blank row sits above and below the content. Content lines sit two spaces
    in from each side, and the right padding ignores colour codes. Content and
    bottom-edge text that would not fit end in "…", so the right edge always
    lines up.

    Args:
        lines: The content to show inside the frame.
        width: The terminal's width for this refresh. The frame is never wider
            than MAX_FRAME_WIDTH.
        top_right_text: The board clock or the quota reading age for the top
            edge. When it is empty, the top edge runs unbroken to the corner.
        bottom_text: The text set into the bottom edge, or None for a plain edge.
        frame_style: The terminal colour for the border and the bottom-edge
            text, or None to draw the frame without colour.
        title: The label in the top-left edge.
    """
    width = min(width, MAX_FRAME_WIDTH)
    # The left corner and the title at the start of the top edge.
    top_start = f"╭─ {title} "
    # The clock or reading age, when there is one, and the right corner at the
    # end of the top edge.
    top_end = f" {top_right_text} ─╮" if top_right_text else "╮"
    # The dashes that fill the top edge between the title and the right-hand text.
    top_dashes = "─" * max(0, width - len(top_start) - len(top_end))
    # The whole top edge, redrawn with colours below when the frame has a style.
    top = top_start + top_dashes + top_end

    if frame_style:
        top = (
            f"{frame_style}╭─ {RESET_STYLE}{AMBER_STYLE}{title}{RESET_STYLE}"
            f"{frame_style} {top_dashes}"
        )
        if top_right_text:
            top += (
                f" {RESET_STYLE}{AMBER_STYLE}{top_right_text}"
                f"{RESET_STYLE}{frame_style} ─╮{RESET_STYLE}"
            )
        else:
            top += f"╮{RESET_STYLE}"

    # The corners, the dashes beside the text and the spaces around it use six
    # columns.
    bottom_start = (
        f"╰─ {shorten_visible(bottom_text, width - 6)} "
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
        short_line = shorten_visible(line, content_width)
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


def shorten_visible(line: str, width: int) -> str:
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
