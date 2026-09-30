"""Check the board's recovery from HCOM process failures."""

import io
import json
import os
import subprocess

import pytest
from agent_board import cli


def test_missing_hcom_returns_one_without_hiding_cursor(monkeypatch) -> None:
    """A missing command exits before changing the terminal display."""
    output = io.StringIO()
    errors = io.StringIO()
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setattr(cli.sys, "stderr", errors)

    assert cli.main() == 1
    assert output.getvalue() == ""
    assert (
        errors.getvalue() == "agent-board: hcom is not installed or is not on PATH.\n"
    )


@pytest.mark.parametrize("colour", [False, True])
def test_nonzero_hcom_exit_is_shown_on_the_board(monkeypatch, colour: bool) -> None:
    """A failed HCOM command stays in an uncoloured frame until the next refresh."""
    output = io.StringIO()

    def failed_hcom(*args, **kwargs):
        """Return the exit status and error text from one failed listing."""
        return subprocess.CompletedProcess(
            args[0], 2, "", "listing failed\nretry later\n"
        )

    def stop_after_refresh(seconds):
        """End the board after one refresh."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", failed_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_refresh)
    monkeypatch.setattr(cli.time, "strftime", lambda format: "12:34:56")
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setattr(output, "isatty", lambda: colour)

    assert cli.main() == 0
    assert "╭─ ✻ agent board " in output.getvalue()
    assert "12:34:56 ─╮" in output.getvalue()
    assert "│" + " " * 78 + "│\n│  HCOM error: listing failed" in output.getvalue()
    assert "│  retry later" in output.getvalue()
    assert "│" + " " * 78 + "│\n╰" in output.getvalue()
    assert "╰" + "─" * 78 + "╯" in output.getvalue()
    assert (
        "\x1b["
        not in output.getvalue().split(cli.CLEAR_SCREEN)[1].split(cli.SHOW_CURSOR)[0]
    )
    assert output.getvalue().endswith(cli.SHOW_CURSOR + "\n")


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ([{"status": "listening", "tag": "Team-scout"}], "missing 'directory'"),
        (None, "'NoneType' object is not iterable"),
    ],
)
def test_unreadable_listing_is_reported_separately(
    monkeypatch, listing, expected
) -> None:
    """A listing with an unexpected shape does not look like an HCOM failure."""
    output = io.StringIO()

    def unreadable_hcom(*args, **kwargs):
        """Return one listing whose fields the board cannot read."""
        return subprocess.CompletedProcess(args[0], 0, json.dumps(listing), "")

    def stop_after_refresh(seconds):
        """End the board after one refresh."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", unreadable_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_refresh)
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    assert f"│  Unexpected hcom listing: {expected}" in output.getvalue()
    assert "╰" + "─" * 78 + "╯" in output.getvalue()


def test_hcom_disappearing_shows_error_and_restores_cursor(monkeypatch) -> None:
    """An HCOM launch failure stays visible until the next refresh."""
    output = io.StringIO()

    def missing_hcom(*args, **kwargs):
        """Simulate HCOM disappearing after the initial PATH check."""
        raise OSError("hcom disappeared")

    def stop_after_refresh(seconds):
        """End the board after its first bounded refresh."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", missing_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_refresh)
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda: os.terminal_size((80, 24))
    )
    monkeypatch.setattr(cli.sys, "stdout", output)

    result = cli.main()

    assert result == 0
    assert "│  HCOM error: hcom disappeared" in output.getvalue()
    assert output.getvalue().endswith(cli.SHOW_CURSOR + "\n")


def test_each_refresh_uses_current_width_and_time(monkeypatch) -> None:
    """A resized pane and changed clock appear on the next refresh."""
    output = io.StringIO()
    sizes = iter([os.terminal_size((60, 24)), os.terminal_size((70, 24))])
    times = iter(["12:34:56", "12:34:58"])
    refreshes = 0

    def listed_hcom(*args, **kwargs):
        """Return an empty listing for both refreshes."""
        return subprocess.CompletedProcess(args[0], 0, "[]", "")

    def stop_after_two_refreshes(seconds):
        """Allow one more refresh before closing the board."""
        nonlocal refreshes
        refreshes += 1

        if refreshes == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda: next(sizes))
    monkeypatch.setattr(cli.time, "strftime", lambda format: next(times))
    monkeypatch.setattr(cli.subprocess, "run", listed_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_two_refreshes)
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    assert output.getvalue().count(cli.CLEAR_SCREEN) == 2
    assert "╭─ ✻ agent board " in output.getvalue()
    assert "12:34:56 ─╮" in output.getvalue()
    assert "12:34:58 ─╮" in output.getvalue()

    frames = output.getvalue().split(cli.CLEAR_SCREEN)[1:]
    assert len(frames[0].splitlines()[1]) == 60
    assert len(frames[1].splitlines()[1]) == 70
