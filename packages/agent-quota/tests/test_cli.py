"""Check the command's table, JSON contract and per-account failures."""

import json
import signal

import pytest
from agent_quota import cli


def reading(provider, account, error=None):
    """Make a fixed probe result without starting either provider's command."""
    if error is not None:
        return {"provider": provider, "account": account, "error": error}

    return {
        "provider": provider,
        "account": account,
        "checked_at": 1735689600,
        "session": {
            "remaining_percent": 75,
            "resets_at": 1735707600,
            "window_seconds": 18000,
        },
        "week": {
            "remaining_percent": 60,
            "resets_at": 1736294400,
            "window_seconds": 604800,
        },
    }


def test_table_shows_four_accounts_and_both_pace_windows(monkeypatch, capsys):
    """Each account has a row with the used share and reset time."""
    monkeypatch.setattr(cli.probe, "account_usage", reading)
    monkeypatch.setattr(cli.time, "time", lambda: 1735689600)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)

    assert cli.main([]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 5
    assert "5h used" in lines[0] and "week used" in lines[0]
    assert [line[:12].strip() for line in lines[1:]] == [
        "claude",
        "claude (2)",
        "codex",
        "codex (2)",
    ]
    assert all("↻" in line for line in lines[1:])
    assert all("┃██░░░░░░░  25%" in line for line in lines[1:])
    assert all("┃███░░░░░░  40%" in line for line in lines[1:])
    assert "\033[" not in "\n".join(lines)


def test_unavailable_account_keeps_other_rows_and_exits_zero(monkeypatch, capsys):
    """One failed probe appears on its row while the other accounts remain."""

    def account_usage(provider, account):
        """Fail only the second Claude account."""
        return reading(
            provider,
            account,
            "sign in required" if (provider, account) == ("claude", "2") else None,
        )

    monkeypatch.setattr(cli.probe, "account_usage", account_usage)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)

    assert cli.main([]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "claude (2)" in lines[2]
    assert "unavailable: sign in required" in lines[2]
    assert "25%" in lines[1] and "25%" in lines[3] and "25%" in lines[4]


@pytest.mark.parametrize("no_color", [None, "", "1"])
def test_colour_is_off_when_piped_or_no_color_is_set(monkeypatch, capsys, no_color):
    """A pipe and NO_COLOR each suppress all terminal escape codes."""
    monkeypatch.setattr(cli.probe, "account_usage", reading)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: no_color is not None)
    if no_color is not None:
        monkeypatch.setenv("NO_COLOR", no_color)
    else:
        monkeypatch.delenv("NO_COLOR", raising=False)

    assert cli.main([]) == 0

    assert "\033[" not in capsys.readouterr().out


def test_colour_appears_on_a_terminal_without_no_color(monkeypatch, capsys):
    """A terminal uses escape codes when NO_COLOR is absent."""
    monkeypatch.setattr(cli.probe, "account_usage", reading)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert cli.main([]) == 0

    assert "\033[" in capsys.readouterr().out


def test_json_has_used_percentages_utc_times_and_errors(monkeypatch, capsys):
    """JSON has the published shape and never prints the readable table."""

    def account_usage(provider, account):
        """Make one account unavailable for the JSON error branch."""
        return reading(
            provider,
            account,
            "unavailable" if (provider, account) == ("codex", "2") else None,
        )

    monkeypatch.setattr(cli.probe, "account_usage", account_usage)

    assert cli.main(["--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    accounts = output["data"]["accounts"]
    assert len(accounts) == 4
    assert accounts[0] == {
        "provider": "claude",
        "account": "default",
        "checked_at": "2025-01-01T00:00:00+00:00",
        "session": {
            "used_percent": 25,
            "resets_at": "2025-01-01T05:00:00+00:00",
            "window_seconds": 18000,
        },
        "week": {
            "used_percent": 40,
            "resets_at": "2025-01-08T00:00:00+00:00",
            "window_seconds": 604800,
        },
    }
    assert accounts[3] == {
        "provider": "codex",
        "account": "2",
        "checked_at": None,
        "error": "unavailable",
    }


def test_sigterm_raises_system_exit_and_restores_handler(monkeypatch):
    """Termination unwinds the active probe and restores the caller's handler."""
    original_handler = signal.getsignal(signal.SIGTERM)

    def account_usage(_provider, _account):
        """Simulate termination while a probe is running."""
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(cli.probe, "account_usage", account_usage)

    with pytest.raises(SystemExit, match="143"):
        cli.main([])

    assert signal.getsignal(signal.SIGTERM) == original_handler


def test_unknown_reset_time_is_null_in_json_and_blank_in_table():
    """A window with no reset time gives null in JSON and no reset label in the table."""
    item = reading("claude", "default")
    item["session"]["resets_at"] = 0

    account = cli.account_data(item)

    assert account["session"]["resets_at"] is None
    assert "↻" not in cli.render_window(account["session"], 1735689600, False)


def test_iso_time_uses_utc_for_fractional_timestamps():
    """Saved check times retain their precision with an explicit UTC offset."""
    assert cli.iso_time(1735689600.5) == "2025-01-01T00:00:00.500000+00:00"


def test_used_percent_is_a_whole_number_for_fractional_readings():
    """A fractional remaining share rounds to the same whole number the table shows."""
    item = reading("claude", "default")
    item["session"]["remaining_percent"] = 43.0
    item["week"]["remaining_percent"] = 42.6

    account = cli.account_data(item)

    assert account["session"]["used_percent"] == 57
    assert isinstance(account["session"]["used_percent"], int)
    assert account["week"]["used_percent"] == 57
