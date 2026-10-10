"""Check how team-send picks one live teammate and when it refuses."""

import json
import subprocess

import pytest
from team_send import cli


def agent(
    name: str,
    *,
    tag: str = "agent-tools-scout",
    status: str = "active",
) -> dict[str, str]:
    """Build one HCOM list entry."""
    return {
        "name": name,
        "base_name": name.rsplit("-", 1)[-1],
        "tag": tag,
        "status": status,
    }


@pytest.fixture
def hcom(monkeypatch):
    """Provide the sender environment and a stubbed HCOM list command."""
    calls = []
    listing = [agent("agent-tools-scout-peko")]

    def run(command, **kwargs):
        """Record the lookup and return the test's listing."""
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(listing), "")

    monkeypatch.setenv("HCOM_TAG", "agent-tools-implementer")
    monkeypatch.setenv("HCOM_NAME", "agent-tools-implementer-safe")
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", run)
    return listing, calls


def test_single_live_peer_is_selected_by_exact_tag(hcom, capsys) -> None:
    """Only a live agent whose tag equals the target tag exactly is picked."""
    listing, calls = hcom
    listing.extend(
        [
            agent("old-scout", status="inactive"),
            agent("unknown-scout", status="unknown"),
            agent("other-scout", tag="other-scout"),
            agent("agent-tools-scout-extra", tag="agent-tools-scout-extra"),
        ]
    )

    assert (
        cli.main(
            ["scout", "--intent", "request", "--reply-to", "42", "--thread", "work"]
        )
        == 0
    )

    assert capsys.readouterr().out == "Would send to agent-tools-scout-peko\n"

    assert calls == [
        (
            ["hcom", "list", "--json"],
            {"capture_output": True, "text": True, "timeout": 10, "check": False},
        )
    ]


@pytest.mark.parametrize("status", ["active", "listening", "blocked"])
def test_each_live_status_matches(hcom, capsys, status: str) -> None:
    """Active, listening and blocked teammates can all receive a message."""
    listing, _ = hcom
    listing[0]["status"] = status

    assert cli.main(["scout", "--intent", "inform"]) == 0
    assert "agent-tools-scout-peko" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("role", "environment", "expected"),
    [
        ("scout", {"HCOM_TAG": None}, "HCOM_TAG is unset"),
        ("scout", {"HCOM_NAME": None}, "HCOM_NAME is unset"),
        ("scout", {"HCOM_TAG": "implementer"}, "must end in a team role"),
        ("scout", {"HCOM_TAG": "agent-tools-other"}, "must end in a team role"),
        ("unknown", {}, "Unknown role: unknown"),
        ("implementer", {}, "your own role"),
    ],
)
def test_invalid_input_refuses_before_listing(
    hcom, monkeypatch, capsys, role: str, environment: dict, expected: str
) -> None:
    """Invalid sender or role never calls HCOM."""
    _, calls = hcom

    for key, value in environment.items():
        if value is None:
            monkeypatch.delenv(key)
        else:
            monkeypatch.setenv(key, value)

    assert cli.main([role, "--intent", "request"]) == 1
    output = capsys.readouterr()
    assert expected in output.err
    assert output.out == ""
    assert calls == []


def test_ack_requires_reply_to(hcom, capsys) -> None:
    """An acknowledgement without its event ID is refused locally."""
    _, calls = hcom

    assert cli.main(["scout", "--intent", "ack"]) == 1
    assert "--intent ack requires --reply-to" in capsys.readouterr().err
    assert calls == []


def test_missing_hcom_refuses(hcom, monkeypatch, capsys) -> None:
    """A missing executable is reported without launching a lookup."""
    _, calls = hcom
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    assert cli.main(["scout", "--intent", "request"]) == 1
    assert "not on PATH" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            subprocess.CompletedProcess([], 2, "", "list failed\nretry"),
            "list failed retry",
        ),
        (subprocess.CompletedProcess([], 0, "not json", ""), "Unreadable HCOM list"),
        (subprocess.CompletedProcess([], 0, "{}", ""), "expected a list"),
        (
            subprocess.CompletedProcess([], 0, '[{"tag": "agent-tools-scout"}]', ""),
            "Unreadable HCOM list",
        ),
    ],
)
def test_failed_or_unreadable_listing_refuses(
    hcom, monkeypatch, capsys, result, expected: str
) -> None:
    """Failed and malformed HCOM listings cannot select a recipient."""
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: result)

    assert cli.main(["scout", "--intent", "request"]) == 1
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    "error", [OSError("gone"), subprocess.TimeoutExpired("hcom", 10)]
)
def test_hcom_launch_error_refuses(hcom, monkeypatch, capsys, error) -> None:
    """An unavailable or timed-out HCOM process gives one reason."""

    def fail(*args, **kwargs):
        """Simulate a process failure."""
        raise error

    monkeypatch.setattr(cli.subprocess, "run", fail)

    assert cli.main(["scout", "--intent", "request"]) == 1
    assert "HCOM list failed" in capsys.readouterr().err


def test_no_match_names_target_tag(hcom, capsys) -> None:
    """The refusal identifies the exact missing team role."""
    listing, _ = hcom
    listing.clear()

    assert cli.main(["scout", "--intent", "request"]) == 1
    assert "agent-tools-scout" in capsys.readouterr().err


def test_multiple_matches_list_names(hcom, capsys) -> None:
    """Two live peers for one role are refused, and both are named."""
    listing, _ = hcom
    listing.append(agent("agent-tools-scout-nova", status="blocked"))

    assert cli.main(["scout", "--intent", "request"]) == 1
    error = capsys.readouterr().err
    assert "agent-tools-scout-peko" in error
    assert "agent-tools-scout-nova" in error


def test_json_refusal_is_envelope(hcom, capsys) -> None:
    """Structured refusals use the shared ok/error shape."""
    listing, _ = hcom
    listing.clear()

    assert cli.main(["scout", "--intent", "request", "--json"]) == 1
    output = capsys.readouterr()

    assert json.loads(output.out) == {
        "ok": False,
        "error": "No live teammate for agent-tools-scout.",
    }

    assert output.err == ""


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["scout", "--json"], "the following arguments are required: --intent"),
        (
            ["scout", "--intent", "invalid", "--json"],
            "argument --intent: invalid choice: 'invalid'",
        ),
    ],
)
def test_json_usage_errors_use_refusal_envelope(
    hcom, capsys, arguments: list[str], expected: str
) -> None:
    """With --json, invalid arguments print the JSON refusal and exit 2 without calling hcom."""
    _, calls = hcom

    with pytest.raises(SystemExit) as error:
        cli.main(arguments)

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert error.value.code == 2
    assert set(payload) == {"ok", "error"}
    assert payload["ok"] is False
    assert payload["error"].startswith(expected)
    assert output.err == ""
    assert calls == []


def test_plain_usage_error_keeps_argparse_output(hcom, capsys) -> None:
    """A missing required option still shows argparse usage on stderr."""
    _, calls = hcom

    with pytest.raises(SystemExit) as error:
        cli.main(["scout"])

    output = capsys.readouterr()
    assert error.value.code == 2
    assert output.out == ""
    assert output.err.startswith("usage: ")
    assert "the following arguments are required: --intent" in output.err
    assert calls == []


def test_nested_team_tag_replaces_only_role(hcom, monkeypatch, capsys) -> None:
    """A team label in the sender's tag is kept in the target tag."""
    listing, _ = hcom
    monkeypatch.setenv("HCOM_TAG", "agent-tools-blue-implementer")
    listing[:] = [agent("agent-tools-blue-scout-nova", tag="agent-tools-blue-scout")]

    assert cli.main(["scout", "--intent", "request"]) == 0
    assert "agent-tools-blue-scout-nova" in capsys.readouterr().out
