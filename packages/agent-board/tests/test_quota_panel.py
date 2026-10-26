"""Check that saved quota readings remain clear across panel widths."""

from datetime import datetime, timezone

import pytest
from agent_board.quota import AccountReading, QuotaSnapshot, QuotaWindow
from agent_board.quota_panel import render_quota_panel

# Keep reset times and reading ages fixed in every panel assertion.
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("width", [30, 80])
def test_panel_shows_unchecked_and_checking_states(width: int) -> None:
    """The heading reports a running check even before an account appears."""
    unchecked = QuotaSnapshot(accounts={}, error=None, checking=False)
    checking = QuotaSnapshot(accounts={}, error=None, checking=True)

    assert render_quota_panel(unchecked, width=width, now=NOW) == [
        "Quota",
        "Not checked yet",
    ]
    assert render_quota_panel(checking, width=width, now=NOW) == [
        "Quota · Checking…",
        "Not checked yet",
    ]


@pytest.mark.parametrize("width", [30, 80])
def test_panel_shows_usage_resets_and_reading_age(width: int) -> None:
    """Every account keeps its usage, time left and reading age together."""
    claude = AccountReading(
        provider="claude",
        account="work",
        checked_at="2026-10-26T11:57:00+00:00",
        session=QuotaWindow(20, "2026-10-26T14:10:00+00:00", 18000),
        week=QuotaWindow(40, "2026-10-30T12:00:00+00:00", 604800),
        error=None,
    )
    codex = AccountReading(
        provider="codex",
        account="personal",
        checked_at="2026-10-26T11:00:00+00:00",
        session=QuotaWindow(90, None, 18000),
        week=QuotaWindow(15, None, 604800),
        error=None,
    )
    snapshot = QuotaSnapshot(
        accounts={("codex", "personal"): codex, ("claude", "work"): claude},
        error=None,
        checking=False,
    )

    assert render_quota_panel(snapshot, width=width, now=NOW) == [
        "Quota",
        "Claude · work",
        "  session 20% · week 40%",
        "  resets 2h 10m / 4d",
        "  checked 3m ago",
        "Codex · personal",
        "  session 90% · week 15%",
        "  resets unknown / unknown",
        "  checked 1h ago",
    ]
    assert all(
        len(line) <= width
        for line in render_quota_panel(snapshot, width=width, now=NOW)
    )


@pytest.mark.parametrize("width", [30, 80])
def test_panel_shows_account_and_whole_check_errors(width: int) -> None:
    """An account's error and a missing agent-quota each show on their own row."""
    reading = AccountReading(
        provider="claude",
        account="work",
        checked_at=None,
        session=None,
        week=None,
        error="Provider unavailable",
    )
    snapshot = QuotaSnapshot(
        accounts={("claude", "work"): reading},
        error="[Errno 2] No such file or directory: 'agent-quota'",
        checking=False,
    )

    lines = render_quota_panel(snapshot, width=width, now=NOW)

    assert lines[:4] == [
        "Quota",
        "Claude · work",
        "  Error: Provider unavailable",
        "  check time unknown",
    ]
    assert lines[4] == (
        "Error: agent-quota isn't inst…"
        if width == 30
        else "Error: agent-quota isn't installed, so quota can't be checked"
    )


def test_panel_shortens_long_account_names_and_other_errors() -> None:
    """Long provider messages occupy one line and end at the frame width."""
    reading = AccountReading(
        provider="codex",
        account="a-very-long-account-name",
        checked_at=None,
        session=None,
        week=None,
        error="provider\nfailed after a long request",
    )
    snapshot = QuotaSnapshot(
        accounts={("codex", reading.account): reading},
        error="provider\nfailed",
        checking=False,
    )

    lines = render_quota_panel(snapshot, width=30, now=NOW)

    assert lines[1] == "Codex · a-very-long-account-n…"
    assert lines[2] == "  Error: provider failed afte…"
    assert lines[-1] == "Error: provider failed"
