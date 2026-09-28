"""Check the board's recovery from HCOM process failures."""

import io
import json
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


def test_nonzero_hcom_exit_is_shown_on_the_board(monkeypatch) -> None:
    """A failed HCOM command appears on the board until the next refresh."""
    output = io.StringIO()

    def failed_hcom(*args, **kwargs):
        """Return the exit status and error text from one failed listing."""
        return subprocess.CompletedProcess(args[0], 2, "", "listing failed\n")

    def stop_after_refresh(seconds):
        """End the board after one refresh."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", failed_hcom)
    monkeypatch.setattr(cli.time, "sleep", stop_after_refresh)
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    assert "HCOM error: listing failed" in output.getvalue()
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
    monkeypatch.setattr(cli.sys, "stdout", output)

    assert cli.main() == 0
    assert f"Unexpected hcom listing: {expected}" in output.getvalue()


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
    monkeypatch.setattr(cli.sys, "stdout", output)

    result = cli.main()

    assert result == 0
    assert "HCOM error: hcom disappeared" in output.getvalue()
    assert output.getvalue().endswith(cli.SHOW_CURSOR + "\n")
