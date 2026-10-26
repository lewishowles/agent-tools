"""Check Claude and Codex account quota in the background for the board."""

import json
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

# The shortest gap between automatic quota checks while a team is listed.
# agent-quota keeps Codex readings for 300 seconds and Claude readings for 600,
# counted from when its probe finishes. Waiting a little longer than 300 means
# each check finds the Codex reading expired, and every second check finds the
# Claude reading expired, so Codex is asked about every five minutes and Claude
# about every ten.
CHECK_INTERVAL_SECONDS = 310


@dataclass(frozen=True)
class QuotaWindow:
    """One usage limit for an account, such as its current session or its week."""

    # How much of the limit is used, from 0 to 100.
    used_percent: int
    # When the limit resets, as an ISO time, or None when the provider does not say.
    resets_at: str | None
    # How long the limit lasts, in seconds.
    window_seconds: int | float


@dataclass(frozen=True)
class AccountReading:
    """The latest result for one account: its usage, or the error it gave."""

    # The provider, such as claude or codex, and the account within it.
    provider: str
    account: str
    # When agent-quota last asked the provider, as an ISO time, or None when it
    # gives no time.
    checked_at: str | None
    # The account's session and weekly limits, or None when it has an error.
    session: QuotaWindow | None
    week: QuotaWindow | None
    # Why the account could not be read, or None when its usage is present.
    error: str | None


@dataclass(frozen=True)
class QuotaSnapshot:
    """The reader's state for drawing: the readings and how the latest check went."""

    # The latest reading for each provider and account, read-only.
    accounts: Mapping[tuple[str, str], AccountReading]
    # Why the latest check failed as a whole, or None when it succeeded. The
    # readings from the last successful check stay in accounts.
    error: str | None
    # Whether a check is running in the background.
    checking: bool


class QuotaReader:
    """Run quota checks on a background thread and keep the latest readings.

    A failed check keeps the readings from the last check that succeeded.
    """

    def __init__(self) -> None:
        """Start with no readings, so the first check is due straight away."""
        # Guards the fields below, which the check thread also writes.
        self._lock = threading.Lock()
        # The readings from the last successful check.
        self._accounts: dict[tuple[str, str], AccountReading] = {}
        # Why the latest check failed, or None.
        self._error: str | None = None
        # True while a check runs, so a second one does not start.
        self._checking = False
        # When the latest check started, from time.monotonic().
        self._last_started = float("-inf")

    def check_if_due(self, has_team: bool) -> bool:
        """Start a check when one is due, but only while a team is listed.

        Args:
            has_team: Whether the board lists at least one team.

        Returns:
            True when a check starts.
        """
        if not has_team:
            return False

        return self._start_check(force=False)

    def check_now(self) -> bool:
        """Start a check right away, even when no team is listed.

        Returns:
            True when a check starts, or False when one is already running.
        """
        return self._start_check(force=True)

    def snapshot(self) -> QuotaSnapshot:
        """Return the current readings and check state for drawing.

        Returns:
            A snapshot that later checks do not change.
        """
        with self._lock:
            return QuotaSnapshot(
                accounts=MappingProxyType(self._accounts),
                error=self._error,
                checking=self._checking,
            )

    def _start_check(self, *, force: bool) -> bool:
        """Start a check on a background thread unless one is already running.

        Args:
            force: Whether to skip the interval between automatic checks, as
                the r key does.

        Returns:
            True when a check starts.
        """
        # time.monotonic() keeps the interval right when the system clock changes.
        now = time.monotonic()

        with self._lock:
            if self._checking or (
                not force and now - self._last_started < CHECK_INTERVAL_SECONDS
            ):
                return False

            self._checking = True
            self._last_started = now

        threading.Thread(target=self._check, daemon=True).start()
        return True

    def _check(self) -> None:
        """Run one check and store its readings, or only its error on failure."""
        try:
            accounts = _parse_accounts(_run_quota_command())
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            with self._lock:
                self._error = str(error)
                self._checking = False
            return

        with self._lock:
            self._accounts = accounts
            self._error = None
            self._checking = False


def _run_quota_command() -> str:
    """Run agent-quota once and return its JSON output.

    Returns:
        The command's JSON text.

    Raises:
        OSError: The command cannot be started.
        subprocess.TimeoutExpired: The command runs beyond the timeout.
        ValueError: The command reports a failure.
    """
    # A Claude check takes a few seconds; give up after 30 so a stuck provider
    # does not leave the check running forever.
    result = subprocess.run(
        ["agent-quota", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    if result.returncode:
        raise ValueError(
            result.stderr.strip() or f"agent-quota exited {result.returncode}"
        )

    return result.stdout


def _parse_accounts(output: str) -> dict[tuple[str, str], AccountReading]:
    """Read agent-quota's JSON output into one reading per account.

    Args:
        output: The JSON text printed by agent-quota.

    Returns:
        The account readings keyed by provider and account.

    Raises:
        ValueError: The output does not contain usable account readings.
    """
    try:
        document = json.loads(output)
        entries = document["data"]["accounts"]

        if document["ok"] is not True or not isinstance(entries, list):
            raise ValueError("agent-quota returned an invalid account list")

        accounts = {}

        for entry in entries:
            reading = _parse_account(entry)
            accounts[(reading.provider, reading.account)] = reading
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("agent-quota returned invalid JSON") from error

    return accounts


def _parse_account(entry: dict) -> AccountReading:
    """Read one account entry from agent-quota into an AccountReading.

    Args:
        entry: One account object from agent-quota.

    Returns:
        The account's usage, or the error agent-quota gave for it.

    Raises:
        KeyError: The entry is missing a field.
        TypeError: A field has the wrong type.
    """
    provider = entry["provider"]
    account = entry["account"]
    checked_at = entry["checked_at"]

    if not isinstance(provider, str) or not isinstance(account, str):
        raise TypeError("agent-quota returned an invalid account")

    if checked_at is not None and not isinstance(checked_at, str):
        raise TypeError("agent-quota returned an invalid reading time")

    if "error" in entry:
        error = entry["error"]

        if not isinstance(error, str):
            raise TypeError("agent-quota returned an invalid account error")

        return AccountReading(provider, account, checked_at, None, None, error)

    session = _parse_window(entry["session"])
    week = _parse_window(entry["week"])

    return AccountReading(provider, account, checked_at, session, week, None)


def _parse_window(entry: dict) -> QuotaWindow:
    """Read one usage limit from agent-quota into a QuotaWindow.

    Args:
        entry: One session or week object from agent-quota.

    Returns:
        The usage, reset time and length of the limit.

    Raises:
        KeyError: The entry is missing a field.
        TypeError: A field has the wrong type.
    """
    used_percent = entry["used_percent"]
    resets_at = entry["resets_at"]
    window_seconds = entry["window_seconds"]

    if (
        not isinstance(used_percent, int)
        or not isinstance(resets_at, (str, type(None)))
        or not isinstance(window_seconds, (int, float))
    ):
        raise TypeError("agent-quota returned an invalid usage window")

    return QuotaWindow(used_percent, resets_at, window_seconds)
