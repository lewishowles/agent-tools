"""Check how team-send picks and messages one live teammate, and when it refuses."""

import io
import json
import os
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
def message_file(tmp_path):
    """Provide a message file for tests that use the normal send path."""
    # The file that team-send reads and removes after confirmed delivery.
    body_file = tmp_path / "message.txt"
    body_file.write_text("hello teammate\n")
    return body_file


@pytest.fixture
def hcom(monkeypatch):
    """Provide the sender environment and stubbed HCOM commands."""
    calls = []
    listing = [agent("agent-tools-scout-peko")]
    receipt = {"delivered_to": ["peko"], "event_id": 42}

    def run(command, **kwargs):
        """Record each HCOM call and return its configured result."""
        calls.append((command, kwargs))
        output = listing if command[1] == "list" else receipt
        return subprocess.CompletedProcess(command, 0, json.dumps(output), "")

    monkeypatch.setenv("HCOM_TAG", "agent-tools-implementer")
    monkeypatch.setenv("HCOM_NAME", "agent-tools-implementer-safe")
    monkeypatch.delenv("HCOM_TEAM_LABEL", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda command: "/usr/bin/hcom")
    monkeypatch.setattr(cli.subprocess, "run", run)
    return listing, receipt, calls


def test_single_live_peer_is_selected_by_exact_tag(hcom, message_file, capsys) -> None:
    """Only a live agent whose tag equals the target tag exactly is picked."""
    listing, _, calls = hcom
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
            [
                "scout",
                "--intent",
                "request",
                "--reply-to",
                "42",
                "--thread",
                "work",
                "--body-file",
                str(message_file),
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "Sent to agent-tools-scout-peko (event 42).\n"

    assert calls == [
        (
            ["hcom", "list", "--json"],
            {"capture_output": True, "text": True, "timeout": 10, "check": False},
        ),
        (
            [
                "hcom",
                "send",
                "@agent-tools-scout-peko",
                "--intent",
                "request",
                "--reply-to",
                "42",
                "--thread",
                "work",
                "--name",
                "agent-tools-implementer-safe",
                "--json",
            ],
            {
                "input": "hello teammate\n",
                "capture_output": True,
                "text": True,
                "timeout": 10,
                "check": False,
            },
        ),
    ]


@pytest.mark.parametrize("status", ["active", "listening", "blocked"])
def test_each_live_status_matches(hcom, message_file, capsys, status: str) -> None:
    """Active, listening and blocked teammates can all receive a message."""
    listing, _, _ = hcom
    listing[0]["status"] = status

    assert (
        cli.main(["scout", "--intent", "inform", "--body-file", str(message_file)]) == 0
    )
    assert "agent-tools-scout-peko" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("role", "environment", "expected"),
    [
        (
            "scout",
            {"HCOM_TAG": None},
            "HCOM_TAG is unset. This session isn't connected to HCOM",
        ),
        (
            "scout",
            {"HCOM_NAME": None},
            "HCOM_NAME is unset. This session isn't connected to HCOM",
        ),
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
    _, _, calls = hcom

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
    _, _, calls = hcom

    assert cli.main(["scout", "--intent", "ack"]) == 1
    assert "--intent ack requires --reply-to" in capsys.readouterr().err
    assert calls == []


def test_missing_hcom_refuses(hcom, message_file, monkeypatch, capsys) -> None:
    """A missing executable is reported without launching a lookup."""
    _, _, calls = hcom
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
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
    hcom, message_file, monkeypatch, capsys, result, expected: str
) -> None:
    """Failed and malformed HCOM listings cannot select a recipient."""
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: result)

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    "error", [OSError("gone"), subprocess.TimeoutExpired("hcom", 10)]
)
def test_hcom_launch_error_refuses(
    hcom, message_file, monkeypatch, capsys, error
) -> None:
    """An unavailable or timed-out HCOM process gives one reason."""

    def fail(*args, **kwargs):
        """Simulate a process failure."""
        raise error

    monkeypatch.setattr(cli.subprocess, "run", fail)

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
    assert "HCOM list failed" in capsys.readouterr().err


def test_no_match_names_target_tag(hcom, message_file, capsys) -> None:
    """The refusal identifies the exact missing team role."""
    listing, _, _ = hcom
    listing.clear()

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
    assert "agent-tools-scout" in capsys.readouterr().err


def test_multiple_matches_list_names(hcom, message_file, capsys) -> None:
    """Two live peers for one role are refused, and both are named."""
    listing, _, _ = hcom
    listing.append(agent("agent-tools-scout-nova", status="blocked"))

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
    error = capsys.readouterr().err
    assert "agent-tools-scout-peko" in error
    assert "agent-tools-scout-nova" in error


def test_json_refusal_is_envelope(hcom, message_file, capsys) -> None:
    """Structured refusals use the shared ok/error shape."""
    listing, _, _ = hcom
    listing.clear()

    assert (
        cli.main(
            ["scout", "--intent", "request", "--body-file", str(message_file), "--json"]
        )
        == 1
    )
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
    _, _, calls = hcom

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
    _, _, calls = hcom

    with pytest.raises(SystemExit) as error:
        cli.main(["scout"])

    output = capsys.readouterr()
    assert error.value.code == 2
    assert output.out == ""
    assert output.err.startswith("usage: ")
    assert "the following arguments are required: --intent" in output.err
    assert calls == []


def test_nested_team_tag_replaces_only_role(
    hcom, message_file, monkeypatch, capsys
) -> None:
    """A team label in the sender's tag is kept in the target tag."""
    listing, receipt, _ = hcom
    monkeypatch.setenv("HCOM_TAG", "agent-tools-blue-implementer")
    listing[:] = [agent("agent-tools-blue-scout-nova", tag="agent-tools-blue-scout")]
    receipt["delivered_to"] = ["nova"]

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 0
    )
    assert "agent-tools-blue-scout-nova" in capsys.readouterr().out


@pytest.mark.parametrize("sender_team", [None, ""])
def test_named_team_selects_exact_cross_team_tag(
    hcom, message_file, monkeypatch, capsys, sender_team: str | None
) -> None:
    """A named team targets its role using the sender's repository tag."""
    listing, receipt, calls = hcom

    if sender_team is not None:
        monkeypatch.setenv("HCOM_TEAM_LABEL", sender_team)

    listing[:] = [
        agent("agent-tools-green-scout-nova", tag="agent-tools-green-scout"),
        agent("agent-tools-blue-scout-peko", tag="agent-tools-blue-scout"),
    ]
    receipt["delivered_to"] = ["nova"]

    assert (
        cli.main(
            [
                "scout",
                "--intent",
                "inform",
                "--team",
                "green",
                "--body-file",
                str(message_file),
            ]
        )
        == 0
    )
    assert "agent-tools-green-scout-nova" in capsys.readouterr().out
    assert calls[1][0][2] == "@agent-tools-green-scout-nova"


def test_named_team_from_labelled_sender(
    hcom, message_file, monkeypatch, capsys
) -> None:
    """A labelled sender removes its own label before choosing another team."""
    listing, receipt, calls = hcom
    monkeypatch.setenv("HCOM_TAG", "agent-tools-blue-implementer")
    monkeypatch.setenv("HCOM_TEAM_LABEL", "blue")
    listing[:] = [agent("agent-tools-green-scout-nova", tag="agent-tools-green-scout")]
    receipt["delivered_to"] = ["nova"]

    assert (
        cli.main(
            [
                "scout",
                "--intent",
                "inform",
                "--team",
                "green",
                "--body-file",
                str(message_file),
            ]
        )
        == 0
    )
    assert "agent-tools-green-scout-nova" in capsys.readouterr().out
    assert calls[1][0][2] == "@agent-tools-green-scout-nova"


def test_same_role_in_another_team_succeeds(
    hcom, message_file, monkeypatch, capsys
) -> None:
    """A sender can reach their role in another named team."""
    listing, receipt, calls = hcom
    monkeypatch.setenv("HCOM_TAG", "agent-tools-blue-reviewer")
    monkeypatch.setenv("HCOM_TEAM_LABEL", "blue")
    listing[:] = [
        agent("agent-tools-green-reviewer-nova", tag="agent-tools-green-reviewer")
    ]
    receipt["delivered_to"] = ["nova"]

    assert (
        cli.main(
            [
                "reviewer",
                "--intent",
                "inform",
                "--team",
                "green",
                "--body-file",
                str(message_file),
            ]
        )
        == 0
    )
    assert "agent-tools-green-reviewer-nova" in capsys.readouterr().out
    assert calls[1][0][2] == "@agent-tools-green-reviewer-nova"


def test_same_role_in_own_named_team_refuses(hcom, monkeypatch, capsys) -> None:
    """Naming the sender's team cannot send to their own role."""
    _, _, calls = hcom
    monkeypatch.setenv("HCOM_TAG", "agent-tools-blue-reviewer")
    monkeypatch.setenv("HCOM_TEAM_LABEL", "blue")

    assert cli.main(["reviewer", "--intent", "inform", "--team", "blue"]) == 1
    assert "Cannot send to your own role: reviewer." in capsys.readouterr().err
    assert calls == []


def test_named_team_without_match_refuses(
    hcom, message_file, monkeypatch, capsys
) -> None:
    """A named team with no live role reports the exact target tag."""
    listing, _, calls = hcom
    monkeypatch.setenv("HCOM_TEAM_LABEL", "blue")
    monkeypatch.setenv("HCOM_TAG", "agent-tools-blue-implementer")
    listing.clear()

    assert (
        cli.main(
            [
                "scout",
                "--intent",
                "request",
                "--team",
                "green",
                "--body-file",
                str(message_file),
            ]
        )
        == 1
    )
    assert "No live teammate for agent-tools-green-scout." in capsys.readouterr().err
    assert len(calls) == 1


def test_named_team_with_multiple_matches_refuses(hcom, message_file, capsys) -> None:
    """Several live matches for a named team are reported without sending."""
    listing, _, calls = hcom
    listing[:] = [
        agent("agent-tools-green-scout-peko", tag="agent-tools-green-scout"),
        agent("agent-tools-green-scout-nova", tag="agent-tools-green-scout"),
    ]

    assert (
        cli.main(
            [
                "scout",
                "--intent",
                "request",
                "--team",
                "green",
                "--body-file",
                str(message_file),
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert "Several live teammates for agent-tools-green-scout" in error
    assert "agent-tools-green-scout-peko" in error
    assert "agent-tools-green-scout-nova" in error
    assert len(calls) == 1


@pytest.mark.parametrize("label", ["", "-green", "green-", "Green", "green_team"])
def test_invalid_named_team_refuses_before_listing(hcom, capsys, label: str) -> None:
    """An invalid team label cannot lead to an HCOM lookup."""
    _, _, calls = hcom

    assert cli.main(["scout", "--intent", "request", f"--team={label}"]) == 1
    assert "Invalid team label" in capsys.readouterr().err
    assert calls == []


def test_sender_team_label_mismatch_refuses_before_listing(
    hcom, monkeypatch, capsys
) -> None:
    """A stale sender label cannot be used to guess the repository tag."""
    _, _, calls = hcom
    monkeypatch.setenv("HCOM_TEAM_LABEL", "blue")

    assert cli.main(["scout", "--intent", "request", "--team", "green"]) == 1
    assert (
        "HCOM_TAG must have HCOM_TEAM_LABEL just before the role; cannot find the repository tag."
        in capsys.readouterr().err
    )
    assert calls == []


def test_sender_team_without_repository_refuses_before_listing(
    hcom, monkeypatch, capsys
) -> None:
    """A sender tag containing only a team label cannot name a repository."""
    _, _, calls = hcom
    monkeypatch.setenv("HCOM_TAG", "-blue-implementer")
    monkeypatch.setenv("HCOM_TEAM_LABEL", "blue")

    assert cli.main(["scout", "--intent", "request", "--team", "green"]) == 1
    assert (
        "HCOM_TAG has no repository tag before HCOM_TEAM_LABEL"
        in capsys.readouterr().err
    )
    assert calls == []


@pytest.mark.parametrize(
    ("body", "terminal"),
    [
        ("hello", True),
        ("hello", False),
        ("", False),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_missing_body_file_refuses_without_reading_stdin(
    hcom, monkeypatch, capsys, body: str, terminal: bool, json_output: bool
) -> None:
    """A missing file option refuses terminal and piped input without reading it."""
    _, _, calls = hcom
    # Input waiting on stdin, whose read position shows whether it was consumed.
    stdin = io.StringIO(body)
    stdin.isatty = lambda: terminal
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    # The command line without a message file.
    arguments = ["scout", "--intent", "request"]

    if json_output:
        arguments.append("--json")

    assert cli.main(arguments) == 1

    # What the refusal printed.
    output = capsys.readouterr()
    # The form of the command that the refusal must name.
    expected = "team-send <role> --body-file <path>"

    if json_output:
        assert json.loads(output.out) == {
            "ok": False,
            "error": f"Pass a message file with: {expected}.",
        }
        assert output.err == ""
    else:
        assert expected in output.err
        assert output.out == ""

    assert stdin.tell() == 0
    assert calls == []


def test_body_file_sends_its_contents_and_removes_only_that_file(
    hcom, tmp_path, monkeypatch, capsys
) -> None:
    """Confirmed delivery removes the chosen file, leaves its folder intact, and refuses a second send."""
    # The HCOM commands that team-send ran.
    _, _, calls = hcom
    # The report that delivery should remove.
    body_file = tmp_path / "report.txt"
    # A file in the same folder that delivery must not touch.
    other_file = tmp_path / "notes.txt"
    # Stdin that looks like a terminal and holds text. Neither matters when a body file is given.
    stdin = io.StringIO("stale piped report")

    body_file.write_text("fresh report\n")
    other_file.write_text("keep this")
    stdin.isatty = lambda: True
    monkeypatch.setattr(cli.sys, "stdin", stdin)

    assert cli.main(["scout", "--intent", "inform", "--body-file", str(body_file)]) == 0

    assert calls[1][1]["input"] == "fresh report\n"
    assert stdin.tell() == 0
    assert not body_file.exists()
    assert other_file.read_text() == "keep this"
    assert "event 42" in capsys.readouterr().out

    assert cli.main(["scout", "--intent", "inform", "--body-file", str(body_file)]) == 1
    assert "may already have been sent" in capsys.readouterr().err
    assert len(calls) == 2


@pytest.mark.parametrize(
    "change", ["same-size rewrite", "different-size rewrite", "replace"]
)
@pytest.mark.parametrize("json_output", [False, True])
def test_body_file_changed_during_send_is_kept(
    hcom, tmp_path, monkeypatch, capsys, change: str, json_output: bool
) -> None:
    """A delivered report stays in place if its path or contents change during sending."""
    # The HCOM calls recorded by the fixture.
    _, _, calls = hcom
    # The report passed to team-send.
    body_file = tmp_path / "report.txt"
    # The file moved over the report in the "replace" case.
    replacement = tmp_path / "replacement.txt"
    # The fixture's fake HCOM call, which the wrapper below hands on to.
    original_run = cli.subprocess.run
    # The command-line arguments for the send.
    args = ["scout", "--intent", "inform", "--body-file", str(body_file)]

    body_file.write_text("sent report")

    # The report's details before sending, so each change can isolate one difference.
    original_stat = body_file.stat()

    def run(command, **kwargs):
        """Change the report while HCOM handles the send."""
        if command[1] == "send":
            if change == "same-size rewrite":
                body_file.write_text("new version")
                os.utime(
                    body_file,
                    ns=(
                        original_stat.st_atime_ns,
                        original_stat.st_mtime_ns + 1_000_000_000,
                    ),
                )
                assert body_file.stat().st_size == original_stat.st_size
                assert body_file.stat().st_mtime_ns != original_stat.st_mtime_ns
            elif change == "different-size rewrite":
                body_file.write_text("new report with more text")
                os.utime(
                    body_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns)
                )
                assert body_file.stat().st_size != original_stat.st_size
                assert body_file.stat().st_mtime_ns == original_stat.st_mtime_ns
            elif change == "replace":
                replacement.write_text("new version")
                # Matching size and modified time leave only the file identity different.
                os.utime(
                    replacement,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                replacement.replace(body_file)
                assert body_file.stat().st_size == original_stat.st_size
                assert body_file.stat().st_mtime_ns == original_stat.st_mtime_ns
                assert body_file.stat().st_ino != original_stat.st_ino

        return original_run(command, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)

    if json_output:
        args.append("--json")

    assert cli.main(args) == 0

    # What the command printed.
    output = capsys.readouterr()

    # The warning the sender should see.
    warning = (
        "Sent, but the file changed after it was read, so it was kept. "
        "Its current contents have not been sent."
    )

    if json_output:
        assert json.loads(output.out)["data"]["warning"] == warning
        assert output.err == ""
    else:
        assert output.out == "Sent to agent-tools-scout-peko (event 42).\n"
        assert warning in output.err

    assert calls[1][1]["input"] == "sent report"

    assert body_file.read_text() == (
        "new report with more text"
        if change == "different-size rewrite"
        else "new version"
    )


@pytest.mark.parametrize("json_output", [False, True])
def test_body_file_deleted_during_send_warns_not_to_send_it_again(
    hcom, tmp_path, monkeypatch, capsys, json_output: bool
) -> None:
    """A report deleted during sending gets the removal warning after delivery."""
    # The HCOM calls recorded by the fixture.
    _, _, calls = hcom
    # The report passed to team-send.
    body_file = tmp_path / "report.txt"
    # The fixture's fake HCOM call, which the wrapper below hands on to.
    original_run = cli.subprocess.run
    # The command-line arguments for the send.
    args = ["scout", "--intent", "inform", "--body-file", str(body_file)]

    body_file.write_text("sent report")

    def run(command, **kwargs):
        """Delete the report while HCOM handles the send."""
        if command[1] == "send":
            body_file.unlink()

        return original_run(command, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)

    if json_output:
        args.append("--json")

    assert cli.main(args) == 0

    # What the command printed.
    output = capsys.readouterr()
    # The warning shown after the send succeeds.
    warning = json.loads(output.out)["data"]["warning"] if json_output else output.err

    assert f"could not remove body file {body_file}" in warning
    assert "Do not send it again" in warning
    assert not body_file.exists()
    assert calls[1][1]["input"] == "sent report"


def test_symlink_swapped_in_before_body_file_open_is_refused(
    hcom, tmp_path, monkeypatch, capsys
) -> None:
    """A symlink swapped in just before the body file is opened is refused and not sent."""
    # The HCOM calls recorded by the fixture.
    _, _, calls = hcom
    # The report passed to team-send.
    body_file = tmp_path / "report.txt"
    # The file the swapped-in symlink points to.
    target = tmp_path / "target.txt"
    # The real open call, which the wrapper below hands on to.
    original_open = cli.os.open

    body_file.write_text("original report")
    target.write_text("do not send this")

    def swap_before_open(path, flags):
        """Replace the report just before its file descriptor is opened."""
        body_file.unlink()
        body_file.symlink_to(target)
        return original_open(path, flags)

    monkeypatch.setattr(cli.os, "open", swap_before_open)

    assert cli.main(["scout", "--intent", "inform", "--body-file", str(body_file)]) == 1
    assert "ordinary file" in capsys.readouterr().err
    assert body_file.is_symlink()
    assert target.read_text() == "do not send this"
    assert calls == []


@pytest.mark.parametrize("body", ["", "  \n"])
def test_empty_body_file_is_kept(hcom, tmp_path, capsys, body: str) -> None:
    """A blank body file is refused and stays in place."""
    # The HCOM commands that team-send ran.
    _, _, calls = hcom
    # The blank report that should stay in place.
    body_file = tmp_path / "report.txt"

    body_file.write_text(body)

    assert cli.main(["scout", "--intent", "inform", "--body-file", str(body_file)]) == 1

    assert "Message body is empty" in capsys.readouterr().err
    assert body_file.read_text() == body
    assert calls == []


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "symlink", "named pipe", "invalid UTF-8"]
)
def test_invalid_body_file_refuses_without_sending(
    hcom, tmp_path, capsys, kind: str
) -> None:
    """A missing, unsuitable, or unreadable body file is refused before sending."""
    # The HCOM commands that team-send ran.
    _, _, calls = hcom
    # The path passed to team-send, set up below to match the kind of bad file.
    body_file = tmp_path / "report.txt"

    if kind == "directory":
        body_file.mkdir()
    elif kind == "symlink":
        # The file the symlink points at, which must survive the refusal.
        target = tmp_path / "target.txt"

        target.write_text("keep this")
        body_file.symlink_to(target)
    elif kind == "named pipe":
        os.mkfifo(body_file)
    elif kind == "invalid UTF-8":
        body_file.write_bytes(b"\xff")

    assert cli.main(["scout", "--intent", "inform", "--body-file", str(body_file)]) == 1

    # The refusal message that team-send printed.
    error = capsys.readouterr().err

    # The words the refusal must contain for this kind of bad file.
    expected_error = {
        "missing": "may already have been sent",
        "directory": "ordinary file",
        "symlink": "ordinary file",
        "named pipe": "ordinary file",
        "invalid UTF-8": "Cannot read body file",
    }[kind]

    assert expected_error in error
    assert calls == []

    if kind == "symlink":
        assert body_file.is_symlink()
        assert target.read_text() == "keep this"


@pytest.mark.parametrize("refusal", ["own role", "no teammate", "several teammates"])
def test_refusal_before_sending_keeps_body_file(
    hcom, tmp_path, capsys, refusal: str
) -> None:
    """A refusal while choosing the teammate keeps the body file."""
    # The live agents HCOM reports, and the HCOM commands that team-send ran.
    listing, _, calls = hcom
    # The report that the refusal should leave in place.
    body_file = tmp_path / "report.txt"
    # The role to send to, which is the sender's own for the own-role refusal.
    role = "implementer" if refusal == "own role" else "scout"

    body_file.write_text("keep this")

    if refusal == "no teammate":
        listing.clear()
    elif refusal == "several teammates":
        listing.append(agent("agent-tools-scout-nova"))

    assert cli.main([role, "--intent", "inform", "--body-file", str(body_file)]) == 1

    assert body_file.read_text() == "keep this"
    assert all(command[1] != "send" for command, _ in calls)
    assert capsys.readouterr().err


@pytest.mark.parametrize("failure", ["send", "receipt"])
def test_unconfirmed_delivery_keeps_body_file(
    hcom, tmp_path, monkeypatch, capsys, failure: str
) -> None:
    """A failed send or unconfirmed receipt keeps the body file."""
    # The delivery receipt HCOM returns, and the HCOM commands that team-send ran.
    _, receipt, calls = hcom
    # The report that the failed delivery should leave in place.
    body_file = tmp_path / "report.txt"

    body_file.write_text("keep this")

    if failure == "send":
        # The fake HCOM runner, which still answers every call except the send.
        original_run = cli.subprocess.run

        def run(command, **kwargs):
            """Return a failed delivery from the HCOM send call."""
            if command[1] == "send":
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 1, "", "failed")

            return original_run(command, **kwargs)

        monkeypatch.setattr(cli.subprocess, "run", run)
    else:
        receipt["delivered_to"] = ["someone-else"]

    assert cli.main(["scout", "--intent", "inform", "--body-file", str(body_file)]) == 1

    assert body_file.read_text() == "keep this"
    assert len(calls) == 2

    # The refusal the sender saw.
    error = capsys.readouterr().err

    if failure == "receipt":
        assert "The message may still have been delivered." in error
    else:
        assert error


@pytest.mark.parametrize("json_output", [False, True])
def test_removal_failure_reports_success_and_warns(
    hcom, tmp_path, monkeypatch, capsys, json_output: bool
) -> None:
    """A confirmed send remains successful when its file cannot be removed."""
    # The HCOM commands that team-send ran.
    _, _, calls = hcom
    # The delivered report that cannot be removed.
    body_file = tmp_path / "report.txt"
    # The command-line arguments, with or without JSON output.
    args = ["scout", "--intent", "inform", "--body-file", str(body_file)]

    body_file.write_text("keep this")

    def fail_unlink(self):
        """Simulate a file removal error after delivery."""
        raise PermissionError("denied")

    monkeypatch.setattr(cli.Path, "unlink", fail_unlink)

    if json_output:
        args.append("--json")

    assert cli.main(args) == 0

    # The text that team-send printed to stdout and stderr.
    output = capsys.readouterr()

    if json_output:
        # The success envelope printed to stdout.
        payload = json.loads(output.out)

        assert payload["ok"] is True
        assert payload["data"]["event_id"] == 42
        assert "Do not send it again" in payload["data"]["warning"]
        assert output.err == ""
    else:
        assert output.out == "Sent to agent-tools-scout-peko (event 42).\n"
        assert "Do not send it again" in output.err

    assert body_file.read_text() == "keep this"
    assert len(calls) == 2


def test_json_success_names_the_selected_teammate(hcom, message_file, capsys) -> None:
    """Structured success carries the exact recipient and HCOM event ID."""
    _, _, calls = hcom

    assert (
        cli.main(
            ["scout", "--intent", "inform", "--body-file", str(message_file), "--json"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "data": {
            "recipient": "agent-tools-scout-peko",
            "base_name": "peko",
            "tag": "agent-tools-scout",
            "event_id": 42,
        },
    }
    assert len(calls) == 2


@pytest.mark.parametrize("delivered_to", [[], ["nova"], ["peko", "nova"]])
def test_other_delivery_refuses_without_resending(
    hcom, message_file, capsys, delivered_to
) -> None:
    """A receipt must name only the selected agent's base name."""
    _, receipt, calls = hcom
    receipt["delivered_to"] = delivered_to

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
    assert "did not name only the chosen teammate" in capsys.readouterr().err
    assert len(calls) == 2


@pytest.mark.parametrize("receipt", ["not json", "{}", '{"delivered_to": ["peko"]}'])
def test_unreadable_send_receipt_refuses(
    hcom, message_file, monkeypatch, capsys, receipt: str
) -> None:
    """A malformed or incomplete receipt cannot confirm delivery."""
    _, _, calls = hcom
    original_run = cli.subprocess.run

    def run(command, **kwargs):
        """Return the test receipt from the send call."""
        if command[1] == "send":
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, receipt, "")

        return original_run(command, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
    assert "Unexpected HCOM send receipt" in capsys.readouterr().err
    assert len(calls) == 2


def test_hcom_send_failure_reports_its_error(
    hcom, message_file, monkeypatch, capsys
) -> None:
    """A failed send reports HCOM's error and is never repeated."""
    _, _, calls = hcom
    original_run = cli.subprocess.run

    def run(command, **kwargs):
        """Return a failed process for the send call."""
        if command[1] == "send":
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command, 2, "", "delivery failed\ntry later"
            )

        return original_run(command, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)

    assert (
        cli.main(["scout", "--intent", "request", "--body-file", str(message_file)])
        == 1
    )
    assert "HCOM send failed: delivery failed try later" in capsys.readouterr().err
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("error", "expected", "delivery_uncertain"),
    [
        (
            subprocess.TimeoutExpired("hcom send", 10),
            "HCOM send timed out after 10 seconds.",
            True,
        ),
        (OSError("cannot start"), "HCOM send failed: cannot start", False),
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_hcom_send_error_keeps_body_file(
    hcom,
    tmp_path,
    monkeypatch,
    capsys,
    error,
    expected: str,
    delivery_uncertain: bool,
    json_output: bool,
) -> None:
    """A send timeout or launch error refuses without removing the report."""
    _, _, calls = hcom
    # The report that must survive the failed send.
    body_file = tmp_path / "report.txt"
    body_file.write_text("keep this")
    # The stubbed HCOM command, which still answers the teammate lookup.
    original_run = cli.subprocess.run

    def run(command, **kwargs):
        """Raise the configured error only when HCOM starts the send."""
        if command[1] == "send":
            calls.append((command, kwargs))
            raise error

        return original_run(command, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run)
    # The command line for the send, with JSON output when the case asks for it.
    arguments = ["scout", "--intent", "inform", "--body-file", str(body_file)]

    if json_output:
        arguments.append("--json")

    assert cli.main(arguments) == 1

    # What the refusal printed.
    output = capsys.readouterr()
    # The refusal text, from whichever output the case uses.
    refusal = json.loads(output.out)["error"] if json_output else output.err

    assert expected in refusal
    assert (
        refusal.rstrip().endswith("The message may still have been delivered.")
        == delivery_uncertain
    )
    assert body_file.read_text() == "keep this"
    assert len(calls) == 2
