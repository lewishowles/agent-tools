"""Check the boxed account table at full and narrow terminal widths."""

from datetime import datetime, timezone

import pytest
from agent_board.board import ANSI_STYLE_PATTERN
from agent_board.quota import AccountReading, QuotaSnapshot, QuotaWindow
from agent_board.quota_panel import render_quota_panel

# A fixed instant makes bar positions and the reading age predictable.
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def _account(
    provider: str,
    account: str,
    *,
    checked_at: str = "2026-10-26T11:57:00+00:00",
) -> AccountReading:
    """Make a reading with known usage and reset times for layout checks."""
    return AccountReading(
        provider=provider,
        account=account,
        checked_at=checked_at,
        session=QuotaWindow(20, "2026-10-26T14:10:00+00:00", 18000),
        week=QuotaWindow(40, "2026-10-30T12:00:00+00:00", 604800),
        error=None,
    )


@pytest.mark.parametrize("width", [30, 60, 80])
def test_box_shows_unchecked_and_checking_states(width: int) -> None:
    """The border reports a running check even before readings appear."""
    unchecked = QuotaSnapshot(accounts={}, error=None, checking=False)
    checking = QuotaSnapshot(accounts={}, error=None, checking=True)

    unchecked_lines = render_quota_panel(unchecked, width=width, now=NOW)
    checking_lines = render_quota_panel(checking, width=width, now=NOW)

    assert "Quota" in unchecked_lines[0]
    assert "not checked yet" in unchecked_lines[0]
    assert "checking…" in checking_lines[0]
    assert "Not checked yet" in unchecked_lines[3]
    assert all(len(line) == width for line in unchecked_lines)
    assert all(len(line) == width for line in checking_lines)


def test_full_box_shows_account_rows_pace_and_local_resets() -> None:
    """The full table keeps each account on one row with the quota bar and reset."""
    claude = _account("claude", "1")
    second = _account("claude", "2", checked_at="2026-10-26T11:00:00+00:00")
    snapshot = QuotaSnapshot(
        accounts={("claude", "2"): second, ("claude", "1"): claude},
        error=None,
        checking=False,
    )

    lines = render_quota_panel(snapshot, width=80, now=NOW)
    local_reset = datetime.fromisoformat(claude.session.resets_at).astimezone()

    assert "checked 3m ago" in lines[0]
    assert "5h used" in lines[2] and "week used" in lines[2]
    assert "claude" in lines[3] and "claude (2)" in lines[4]
    assert "██░░░┃░░░░" in lines[3]
    assert "████┃░░░░░" in lines[3]
    assert f"↻  {local_reset.strftime('%a %H:%M').lower()}" in lines[3]
    assert all(len(line) == 80 for line in lines)


@pytest.mark.parametrize(
    ("width", "has_bar", "has_reset"),
    [(80, True, True), (60, True, False), (30, False, False)],
)
def test_narrow_boxes_remove_detail_before_rows_wrap(
    width: int, has_bar: bool, has_reset: bool
) -> None:
    """Reset times give way to bars, then percentages, as space shrinks."""
    reading = _account("codex", "2")
    snapshot = QuotaSnapshot(
        accounts={("codex", "2"): reading}, error=None, checking=False
    )

    lines = render_quota_panel(snapshot, width=width, now=NOW)

    assert "codex (2)" in lines[3]
    assert "20%" in lines[3] and "40%" in lines[3]
    assert ("┃" in lines[3]) is has_bar
    assert ("↻" in lines[3]) is has_reset
    assert all(len(line) == width for line in lines)
    if width == 30:
        assert "…" not in lines[2]
        assert lines[2].index("week") + len("week") == lines[3].index("40%") + len(
            "40%"
        )


@pytest.mark.parametrize("colour", [False, True])
def test_box_colour_follows_board_setting(colour: bool) -> None:
    """Colour adds a dim header and pace colours only when enabled."""
    reading = _account("claude", "1")
    snapshot = QuotaSnapshot(
        accounts={("claude", "1"): reading}, error=None, checking=False
    )

    lines = render_quota_panel(snapshot, width=80, now=NOW, colour=colour)
    plain_lines = [ANSI_STYLE_PATTERN.sub("", line) for line in lines]

    assert all(len(line) == 80 for line in plain_lines)
    assert any("\x1b[38;2;" in line for line in lines) is colour
    assert ("\x1b[2m" in lines[2]) is colour
    assert ("\x1b[" in "".join(lines)) is colour


def test_account_and_whole_check_errors_have_their_own_rows() -> None:
    """A failed account remains visible when a later whole check also fails."""
    reading = AccountReading(
        provider="claude",
        account="2",
        checked_at=None,
        session=None,
        week=None,
        error="Provider\nunavailable",
    )
    snapshot = QuotaSnapshot(
        accounts={("claude", "2"): reading},
        error="[Errno 2] No such file or directory: 'agent-quota'",
        checking=False,
    )

    lines = render_quota_panel(snapshot, width=80, now=NOW)

    assert "claude (2)" in lines[3]
    assert "unavailable: Provider unavailable" in lines[3]
    assert "Error: agent-quota isn't installed" in lines[4]
    assert all(len(line) == 80 for line in lines)


def test_long_error_and_provider_stay_inside_a_narrow_box() -> None:
    """Long account text is shortened within the border without adding rows."""
    reading = AccountReading(
        provider="a-very-long-provider-name",
        account="1",
        checked_at=None,
        session=None,
        week=None,
        error="provider\nfailed after a long request",
    )
    snapshot = QuotaSnapshot(
        accounts={(reading.provider, "1"): reading},
        error="provider\nfailed",
        checking=False,
    )

    lines = render_quota_panel(snapshot, width=30, now=NOW)

    assert len(lines) == 7
    assert "…" in lines[3]
    assert "Error: provider failed" in lines[4]
    assert all(len(line) == 30 for line in lines)
