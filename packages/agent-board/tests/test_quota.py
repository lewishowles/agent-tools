"""Check quota scheduling and failures without running provider probes."""

import json
import subprocess

import pytest
from agent_board import quota


def test_checks_follow_teams_interval_and_running_check(monkeypatch) -> None:
    """A listed team starts a check once one is due.

    An empty board, the wait between checks and a running check start none.
    """
    # Control when each worker runs without starting a real thread.
    workers = []
    # Advance the scheduler through the interval boundary.
    clock = [100]
    # Return one account with the time supplied by the quota command.
    entry = {
        "provider": "claude",
        "account": "default",
        "checked_at": "2026-10-25T10:00:00+00:00",
        "session": {"used_percent": 20, "resets_at": None, "window_seconds": 18000},
        "week": {"used_percent": 40, "resets_at": None, "window_seconds": 604800},
    }
    # Count launches of the external command.
    commands = []

    class Worker:
        """Capture the background job until the test asks it to finish."""

        def __init__(self, target, daemon):
            """Keep the worker and require a daemon thread so it cannot keep the board open."""
            assert daemon is True
            self.target = target

        def start(self):
            """Queue the job for the test to finish explicitly."""
            workers.append(self.target)

    def listed_quota(command, **kwargs):
        """Return one valid account from the external command."""
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"ok": True, "data": {"accounts": [entry]}}), ""
        )

    monkeypatch.setattr(quota.threading, "Thread", Worker)
    monkeypatch.setattr(quota.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(quota.subprocess, "run", listed_quota)
    reader = quota.QuotaReader()

    assert reader.check_if_due(False) is False
    assert reader.check_if_due(True) is True
    assert reader.check_if_due(True) is False
    assert reader.check_now() is False
    assert reader.snapshot().checking is True
    assert commands == []

    workers.pop()()
    assert reader.snapshot().accounts[("claude", "default")] == quota.AccountReading(
        provider="claude",
        account="default",
        checked_at="2026-10-25T10:00:00+00:00",
        session=quota.QuotaWindow(20, None, 18000),
        week=quota.QuotaWindow(40, None, 604800),
        error=None,
    )
    assert reader.check_if_due(False) is False
    assert ("claude", "default") in reader.snapshot().accounts
    clock[0] += quota.CHECK_INTERVAL_SECONDS - 1
    assert reader.check_if_due(True) is False
    clock[0] += 1
    assert reader.check_if_due(True) is True
    workers.pop()()
    assert commands == [["agent-quota", "--json"], ["agent-quota", "--json"]]


def test_successful_check_replaces_accounts_that_disappear(monkeypatch) -> None:
    """A later complete result removes accounts no longer reported."""

    class Worker:
        """Run each worker in the test thread."""

        def __init__(self, target, daemon):
            """Keep the worker until start is called."""
            self.target = target

        def start(self):
            """Complete the check before the next assertion."""
            self.target()

    # The second command has no account entries.
    outputs = iter(
        [
            {
                "ok": True,
                "data": {
                    "accounts": [
                        {
                            "provider": "claude",
                            "account": "default",
                            "checked_at": None,
                            "error": "unavailable",
                        }
                    ]
                },
            },
            {"ok": True, "data": {"accounts": []}},
        ]
    )

    def quota_result(command, **kwargs):
        """Return the next complete account set."""
        return subprocess.CompletedProcess(command, 0, json.dumps(next(outputs)), "")

    monkeypatch.setattr(quota.threading, "Thread", Worker)
    monkeypatch.setattr(quota.subprocess, "run", quota_result)
    reader = quota.QuotaReader()

    assert reader.check_now() is True
    old_snapshot = reader.snapshot()
    assert list(old_snapshot.accounts) == [("claude", "default")]
    assert reader.check_now() is True
    assert reader.snapshot().accounts == {}
    assert list(old_snapshot.accounts) == [("claude", "default")]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            FileNotFoundError(2, "No such file or directory", "agent-quota"),
            id="missing-command",
        ),
        pytest.param(
            subprocess.CompletedProcess(
                ["agent-quota", "--json"], 7, "", "provider unavailable\n"
            ),
            id="nonzero-exit",
        ),
        pytest.param(
            subprocess.TimeoutExpired(["agent-quota", "--json"], 30),
            id="timeout",
        ),
    ],
)
def test_forced_check_preserves_account_after_command_failure(
    monkeypatch, failure
) -> None:
    """A failed refresh reports the error while keeping the old reading."""

    # Finish each worker synchronously to inspect its stored result.
    class Worker:
        """Run a quota check immediately in this test."""

        def __init__(self, target, daemon):
            """Keep the requested work until start is called."""
            self.target = target

        def start(self):
            """Complete the command before returning."""
            self.target()

    # Return a successful response, then fail as the selected command error.
    responses = iter(
        [
            subprocess.CompletedProcess(
                ["agent-quota", "--json"],
                0,
                json.dumps(
                    {
                        "ok": True,
                        "data": {
                            "accounts": [
                                {
                                    "provider": "codex",
                                    "account": "default",
                                    "checked_at": None,
                                    "error": "account unavailable",
                                }
                            ]
                        },
                    }
                ),
                "",
            ),
            failure,
        ]
    )

    def quota_result(command, **kwargs):
        """Return the account reading or raise the selected command failure."""
        response = next(responses)
        if isinstance(response, (OSError, subprocess.TimeoutExpired)):
            raise response
        return response

    monkeypatch.setattr(quota.threading, "Thread", Worker)
    monkeypatch.setattr(quota.subprocess, "run", quota_result)
    reader = quota.QuotaReader()

    assert reader.check_now() is True
    saved = reader.snapshot().accounts
    assert reader.check_now() is True
    assert reader.snapshot().accounts == saved
    assert reader.snapshot().error == (
        "provider unavailable"
        if isinstance(failure, subprocess.CompletedProcess)
        else str(failure)
    )
    assert reader.snapshot().checking is False


def test_bad_output_becomes_an_error_without_a_crash(monkeypatch) -> None:
    """Output without an account list becomes an error and leaves no check running."""

    # Run the job inline to avoid a timing dependency in the assertion.
    class Worker:
        """Run the worker when it is started."""

        def __init__(self, target, daemon):
            """Keep the worker until start is called."""
            self.target = target

        def start(self):
            """Finish the check for this test."""
            self.target()

    monkeypatch.setattr(quota.threading, "Thread", Worker)
    monkeypatch.setattr(
        quota.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "{}", ""),
    )
    reader = quota.QuotaReader()

    assert reader.check_now() is True
    assert reader.snapshot().error == "agent-quota returned invalid JSON"
    assert reader.snapshot().accounts == {}
    assert reader.snapshot().checking is False
