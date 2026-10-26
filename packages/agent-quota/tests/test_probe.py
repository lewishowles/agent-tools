"""Check quota parsing, saved readings, and the Codex JSON line protocol."""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from agent_quota import probe


def test_claude_reset_phrase_uses_the_next_matching_date():
    """A yearless reset in the future resolves to its UTC timestamp."""
    reset = datetime.now(timezone.utc) + timedelta(days=2)
    phrase = f"{reset:%b} {reset.day} at {reset:%I:%M%p} (UTC)".replace(
        "AM", "am"
    ).replace("PM", "pm")
    expected = reset.replace(second=0, microsecond=0)

    assert probe.claude_reset_epoch(phrase) == int(expected.timestamp())


@pytest.mark.parametrize("phrase", ["tomorrow", "Jan 1 at 2pm (No/Such_Zone)"])
def test_claude_reset_phrase_rejects_invalid_text(phrase):
    """Unrecognised dates and timezones cannot become quota reset times."""
    with pytest.raises(ValueError, match="Claude quota reset is invalid"):
        probe.claude_reset_epoch(phrase)


def test_unstarted_claude_windows_get_full_estimated_lifetimes(monkeypatch):
    """Claude omits reset phrases for unused windows, so estimate both resets."""

    class Server:
        """Stands in for the claude process that prints the usage screen."""

        # The exit code of a check that succeeded.
        returncode = 0

        def communicate(self, timeout):
            """Return a usage screen where neither window has started."""
            assert timeout == 20
            return (
                b"Current session: 0% used\nCurrent week (all models): 0% used\n",
                b"",
            )

    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: Server())
    monkeypatch.setattr(probe, "stop_server", lambda server: b"")
    monkeypatch.setattr(probe.time, "time", lambda: 1000)

    assert probe.claude_usage(None) == [100, 19000, 18000, 100, 605800, 604800]


def test_saved_reading_preserves_format_and_original_check_time(tmp_path, monkeypatch):
    """The new reader accepts the same six-value cache file as the zsh script."""
    cache_file = tmp_path / "claude-default.json"
    quota = [90, 3000, 18000, 80, 700000, 604800]
    monkeypatch.setattr(probe.time, "time", lambda: 1000)

    probe.write_cache(cache_file, quota, 1300)

    assert json.loads(cache_file.read_text()) == {"quota": quota, "expires": 1300}
    assert probe.read_cache(cache_file, 600) == (quota, 700)


@pytest.mark.parametrize(
    "saved",
    [
        {"quota": [90, 3000, 18000, 80, 700000, 604800], "expires": 1000},
        {"quota": [90, 3000, 18000, 80, 700000, 604800], "expires": 1700},
        {"quota": [True, 3000, 18000, 80, 700000, 604800], "expires": 1300},
        {"quota": [90, 3000, 18000], "expires": 1300},
    ],
)
def test_saved_reading_rejects_expired_and_invalid_values(tmp_path, monkeypatch, saved):
    """The cache is used only when both windows and the expiry are valid."""
    cache_file = tmp_path / "codex-default.json"
    cache_file.write_text(json.dumps(saved))
    monkeypatch.setattr(probe.time, "time", lambda: 1000)

    assert probe.read_cache(cache_file, 300) is None


def test_codex_reads_both_windows_from_a_json_line_server(tmp_path, monkeypatch):
    """The probe sends the real RPC sequence and reads window durations by slot."""
    server_script = tmp_path / "codex_server.py"
    server_script.write_text(
        """import json
import sys

initialise = json.loads(sys.stdin.readline())
assert initialise["method"] == "initialize"
print(json.dumps({"id": 1, "result": {}}), flush=True)
assert json.loads(sys.stdin.readline())["method"] == "initialized"
assert json.loads(sys.stdin.readline())["method"] == "account/rateLimits/read"
print(json.dumps({"id": 2, "result": {"rateLimits": {
    "primary": {"usedPercent": 25, "resetsAt": 2000, "windowDurationMins": 300},
    "secondary": {"usedPercent": 40, "resetsAt": 3000, "windowDurationMins": 10080},
}}}), flush=True)
"""
    )
    real_popen = subprocess.Popen

    def start_server(command, **kwargs):
        """Start the stand-in script in place of codex app-server."""
        assert command == ["codex", "app-server"]
        assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)
        return real_popen([sys.executable, str(server_script)], **kwargs)

    monkeypatch.setattr(probe.subprocess, "Popen", start_server)

    assert probe.codex_usage(tmp_path) == [75, 2000, 18000, 60, 3000, 604800]


def test_account_usage_reuses_saved_reading_without_a_new_probe(tmp_path, monkeypatch):
    """A cached reading reports its original check time and both windows."""
    cache_dir = tmp_path / f"hcom-quota-{probe.os.getuid()}"
    cache_dir.mkdir(mode=0o700)
    (cache_dir / "claude-default.json").write_text(
        json.dumps({"quota": [90, 3000, 18000, 80, 700000, 604800], "expires": 1300})
    )
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(probe.time, "time", lambda: 1000)
    monkeypatch.setattr(
        probe, "claude_usage", lambda account_home: pytest.fail("live probe ran")
    )

    assert probe.account_usage("claude", "default") == {
        "provider": "claude",
        "account": "default",
        "checked_at": 700,
        "session": {
            "remaining_percent": 90,
            "resets_at": 3000,
            "window_seconds": 18000,
        },
        "week": {
            "remaining_percent": 80,
            "resets_at": 700000,
            "window_seconds": 604800,
        },
    }


def test_account_usage_checks_and_saves_a_fresh_reading(tmp_path, monkeypatch):
    """A fresh check records the quota in the shared cache until its lifetime ends."""
    quota = [90, 3000, 18000, 80, 700000, 604800]
    checked_at = 1000
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(probe.time, "time", lambda: checked_at)
    monkeypatch.setattr(probe, "claude_usage", lambda account_home: quota)

    result = probe.account_usage("claude", "default")
    cache_file = tmp_path / f"hcom-quota-{probe.os.getuid()}" / "claude-default.json"

    assert result["checked_at"] == checked_at
    assert result["session"]["remaining_percent"] == 90
    assert result["week"]["remaining_percent"] == 80
    assert json.loads(cache_file.read_text()) == {
        "quota": quota,
        "expires": checked_at + probe.CACHE_LIFETIMES["claude"],
    }


def test_account_usage_reports_probe_errors(tmp_path, monkeypatch):
    """A failed account check returns its message for the caller to display."""
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))

    def fail_probe(account_home):
        """Stand in for a quota check that cannot read the account."""
        raise ValueError("Claude account is signed out")

    monkeypatch.setattr(probe, "claude_usage", fail_probe)

    assert probe.account_usage("claude", "default") == {
        "provider": "claude",
        "account": "default",
        "error": "Claude account is signed out",
    }


def test_account_usage_keeps_signed_out_instructions_in_detail(tmp_path, monkeypatch):
    """A named signed-out Claude account keeps sign-in steps out of its short error."""

    class Server:
        """A finished Claude process whose output has no quota windows."""

        # The exit status of a Claude run that worked but printed no quota.
        returncode = 0

        def communicate(self, timeout):
            """Return text from a signed-out account without starting Claude."""
            assert timeout == 20
            return b"Cost summary without usage windows", b""

    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: Server())
    monkeypatch.setattr(probe, "stop_server", lambda server: b"")
    monkeypatch.setattr(probe, "claude_signed_out", lambda env: True)
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))

    result = probe.account_usage("claude", "2")

    assert result["error"] == "Claude account is signed out"
    assert "Sign in with: CLAUDE_CONFIG_DIR=" in result["error_detail"]
    assert "Sign in" not in result["error"]


def test_account_usage_separates_response_and_stderr_from_error(tmp_path, monkeypatch):
    """Raw tool output stays in the diagnostic field, away from the account message."""
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))

    def fail_probe(account_home):
        """Return a failed provider response with separate output streams."""
        raise probe.probe_error(
            "Claude quota request failed", '{"reason":"logged out"}', "login expired"
        )

    monkeypatch.setattr(probe, "claude_usage", fail_probe)

    assert probe.account_usage("claude", "default") == {
        "provider": "claude",
        "account": "default",
        "error": "Claude quota request failed",
        "error_detail": 'response: {"reason":"logged out"}; stderr: login expired',
    }


@pytest.mark.parametrize(
    ("reply", "expected_error"),
    [
        ('{"id":2,"error":{"message":"denied"}}', "Codex rejected the quota request"),
        (None, "Codex closed the connection during the quota request"),
    ],
)
def test_codex_request_failure_keeps_request_number_in_detail(
    tmp_path, monkeypatch, reply, expected_error
):
    """A failed RPC identifies the request in JSON detail, never in the short error."""
    server_script = tmp_path / "codex_server.py"
    server_script.write_text(
        """import json
import sys

json.loads(sys.stdin.readline())
print(json.dumps({"id": 1, "result": {}}), flush=True)
json.loads(sys.stdin.readline())
json.loads(sys.stdin.readline())
if sys.argv[1] != "closed":
    print(sys.argv[1], flush=True)
print("provider diagnostic", file=sys.stderr, flush=True)
"""
    )
    real_popen = subprocess.Popen

    def start_server(command, **kwargs):
        """Use a local server that rejects or closes the second request."""
        assert command == ["codex", "app-server"]
        mode = "closed" if reply is None else reply
        return real_popen([sys.executable, str(server_script), mode], **kwargs)

    monkeypatch.setattr(probe.subprocess, "Popen", start_server)
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))

    result = probe.account_usage("codex", "default")

    assert result["error"] == expected_error
    assert "2" not in result["error"]
    assert "denied" not in result["error"]
    assert "request: 2" in result["error_detail"]
    assert "stderr: provider diagnostic" in result["error_detail"]
    if reply is not None:
        assert "denied" in result["error_detail"]


def test_account_usage_names_unexpected_probe_failures(tmp_path, monkeypatch):
    """An unexpected failure reports only its error type, never the raw details."""
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))

    def fail_write(cache_file, quota, expires):
        """Stand in for a cache write that fails because the disk is full."""
        raise OSError("No space left on device")

    monkeypatch.setattr(
        probe,
        "claude_usage",
        lambda account_home: [90, 3000, 18000, 80, 700000, 604800],
    )
    monkeypatch.setattr(probe, "write_cache", fail_write)

    assert probe.account_usage("claude", "default") == {
        "provider": "claude",
        "account": "default",
        "error": "Quota probe failed: OSError",
    }


def test_account_usage_rejects_an_unsafe_cache_directory(tmp_path, monkeypatch):
    """A cache directory readable by other users cannot hold account readings."""
    cache_dir = tmp_path / f"hcom-quota-{probe.os.getuid()}"
    cache_dir.mkdir()
    cache_dir.chmod(0o755)
    monkeypatch.setattr(probe.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        probe, "claude_usage", lambda account_home: pytest.fail("live probe ran")
    )

    assert probe.account_usage("claude", "default") == {
        "provider": "claude",
        "account": "default",
        "error": "Unsafe quota cache directory",
    }
