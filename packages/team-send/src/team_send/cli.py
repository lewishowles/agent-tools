"""Send an HCOM message to one live teammate in the sender's team or a named team of the same repository."""

import argparse
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

# The team roles a sender can hold and send to. The last part of every team tag
# is one of these.
ROLES = frozenset({"orchestrator", "scout", "implementer", "reviewer"})

# A team label the zsh launcher accepts: lowercase letters and digits, with
# hyphens only between them.
TEAM_LABEL_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")

# The HCOM statuses of an agent that can still receive a message. Inactive and
# unknown agents never count as a match.
LIVE_STATUSES = frozenset({"active", "listening", "blocked"})

# The likely cause and fix when HCOM_TAG or HCOM_NAME is missing. HCOM sets both
# only for a session it launched or resumed, so a session quit and reopened with
# plain claude or codex loses them.
NOT_IN_HCOM_HINT = (
    "This session isn't connected to HCOM. If it was quit and reopened outside HCOM, "
    "quit it and reopen it with: hcom r <name>"
)


class BodyFileError(Exception):
    """Raised when a body file cannot be sent, with the reason to show the sender."""


@dataclass(frozen=True)
class BodyFileRead:
    """A body file's text and the details that show whether the file later changed."""

    # The text read from the file, which is what gets sent.
    text: str
    # The opened file's details, taken just before its text was read.
    file_stat: os.stat_result


def main(argv: list[str] | None = None) -> int:
    """Send a file or piped message to the one live teammate for the requested role.

    With --team, the teammate comes from that named team in the sender's repository.
    With --body-file, the file is removed only after HCOM confirms delivery.
    A refusal or failed send prints the reason and returns 1, and a message is never sent twice.
    Invalid arguments exit with status 2, as argparse does.
    """
    arguments = sys.argv[1:] if argv is None else argv
    parser = _ArgumentParser(
        json_output="--json" in arguments,
        # Only the exact --json flag switches output to JSON, including for usage errors.
        allow_abbrev=False,
        description="Send to one live teammate by role in your HCOM team, or in a named team with --team.",
    )
    parser.add_argument("role", help="orchestrator, scout, implementer, or reviewer")
    parser.add_argument("--intent", required=True, choices=("request", "inform", "ack"))
    parser.add_argument("--reply-to", help="HCOM event ID to reply to")
    parser.add_argument("--thread", help="HCOM thread name")
    parser.add_argument("--team", help="Send to this named team in the same repository")
    parser.add_argument(
        "--body-file",
        type=Path,
        help="Send this file and remove it after confirmed delivery",
    )
    parser.add_argument("--json", action="store_true", help="Print structured output")
    args = parser.parse_args(arguments)

    if args.role not in ROLES:
        return _refuse(
            f"Unknown role: {args.role}. "
            "Choose orchestrator, scout, implementer, or reviewer.",
            args.json,
        )

    if args.team is not None and not TEAM_LABEL_PATTERN.fullmatch(args.team):
        return _refuse(
            "Invalid team label. Use lowercase letters, numbers, and internal hyphens.",
            args.json,
        )

    sender_tag = os.environ.get("HCOM_TAG")
    sender_name = os.environ.get("HCOM_NAME")

    if not sender_tag:
        return _refuse(f"HCOM_TAG is unset. {NOT_IN_HCOM_HINT}", args.json)

    if not sender_name:
        return _refuse(f"HCOM_NAME is unset. {NOT_IN_HCOM_HINT}", args.json)

    prefix, _, sender_role = sender_tag.rpartition("-")

    if not prefix or sender_role not in ROLES:
        return _refuse("HCOM_TAG must end in a team role.", args.json)

    target_prefix = prefix

    # Repository and team names can both contain hyphens, so the repository tag
    # comes from removing the sender's known label rather than from splitting
    # HCOM_TAG. A sender without a label already has the repository as its prefix.
    if args.team is not None:
        sender_team = os.environ.get("HCOM_TEAM_LABEL")

        if sender_team:
            suffix = f"-{sender_team}"

            if not prefix.endswith(suffix):
                return _refuse(
                    "HCOM_TAG must have HCOM_TEAM_LABEL just before the role; cannot find the repository tag.",
                    args.json,
                )

            if len(prefix) == len(suffix):
                return _refuse(
                    "HCOM_TAG has no repository tag before HCOM_TEAM_LABEL.", args.json
                )

            target_prefix = prefix[: -len(suffix)]

        target_prefix = f"{target_prefix}-{args.team}"

    target_tag = f"{target_prefix}-{args.role}"

    if target_tag == sender_tag:
        return _refuse(f"Cannot send to your own role: {args.role}.", args.json)

    if args.intent == "ack" and not args.reply_to:
        return _refuse("--intent ack requires --reply-to.", args.json)

    # The body file's text and details, when the message comes from a file.
    body_file_read = None

    # The message text, from the body file or from stdin.
    if args.body_file is not None:
        try:
            body_file_read = _read_body_file(args.body_file)
        except BodyFileError as error:
            return _refuse(str(error), args.json)

        body = body_file_read.text
    else:
        if sys.stdin.isatty():
            return _refuse(
                "Pipe a message body to stdin or use --body-file.", args.json
            )

        body = sys.stdin.read()

    if not body.strip():
        return _refuse("Message body is empty.", args.json)

    if shutil.which("hcom") is None:
        return _refuse("hcom is not installed or is not on PATH.", args.json)

    try:
        result = subprocess.run(
            ["hcom", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _refuse(f"HCOM list failed: {error}", args.json)

    if result.returncode:
        detail = result.stderr.strip() or f"hcom exited {result.returncode}"
        return _refuse(f"HCOM list failed: {_one_line(detail)}", args.json)

    try:
        agents = json.loads(result.stdout)

        if not isinstance(agents, list):
            raise TypeError("expected a list of agents")

        matches = []

        for agent in agents:
            if not isinstance(agent, dict):
                raise TypeError("expected agent objects")

            if agent["tag"] == target_tag and agent["status"] in LIVE_STATUSES:
                if not isinstance(agent["name"], str) or not agent["name"]:
                    raise ValueError("expected a non-empty agent name")

                if not isinstance(agent["base_name"], str) or not agent["base_name"]:
                    raise ValueError("expected a non-empty agent base_name")

                matches.append(agent)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        return _refuse(f"Unreadable HCOM list: {_one_line(str(error))}", args.json)

    if not matches:
        return _refuse(f"No live teammate for {target_tag}.", args.json)

    if len(matches) > 1:
        names = ", ".join(agent["name"] for agent in matches)
        return _refuse(f"Several live teammates for {target_tag}: {names}.", args.json)

    recipient = matches[0]
    command = ["hcom", "send", f"@{recipient['name']}", "--intent", args.intent]

    if args.reply_to:
        command.extend(["--reply-to", args.reply_to])

    if args.thread:
        command.extend(["--thread", args.thread])

    command.extend(["--name", sender_name, "--json"])

    try:
        result = subprocess.run(
            command,
            input=body,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return _refuse(f"HCOM send failed: {_one_line(str(error))}", args.json)

    if result.returncode:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"hcom exited {result.returncode}"
        )
        return _refuse(f"HCOM send failed: {_one_line(detail)}", args.json)

    try:
        receipt = json.loads(result.stdout)

        if not isinstance(receipt, dict):
            raise TypeError("expected a delivery receipt")

        # HCOM's receipt lists base names, so the chosen agent must be its only entry.
        if receipt.get("delivered_to") != [recipient["base_name"]]:
            raise ValueError("delivery did not name only the chosen teammate")

        event_id = receipt.get("event_id")

        if type(event_id) is not int:
            raise ValueError("expected an integer event ID")
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        output = _one_line(result.stdout)
        detail = f"{error} ({output})" if output else str(error)
        return _refuse(
            f"Unexpected HCOM send receipt: {detail}. The message may still have been delivered.",
            args.json,
        )

    # The warning for the sender when a delivered body file was kept or could not be removed.
    warning = None

    if args.body_file is not None:
        warning = _finish_body_file(args.body_file, body_file_read.file_stat)

    if args.json:
        # The delivery details in the success output.
        data = {
            "recipient": recipient["name"],
            "base_name": recipient["base_name"],
            "tag": target_tag,
            "event_id": event_id,
        }

        if warning:
            data["warning"] = warning

        print(json.dumps({"ok": True, "data": data}))
    else:
        print(f"Sent to {recipient['name']} (event {event_id}).")

        if warning:
            print(f"team-send: {warning}", file=sys.stderr)

    return 0


def _read_body_file(path: Path) -> BodyFileRead:
    """Read an ordinary file as the message body, along with the details that identify it.

    Args:
        path: The file the sender passed with --body-file.

    Returns:
        The file's text, which may be blank, and its details; the caller refuses a blank body.

    Raises:
        BodyFileError: The file is missing, is not an ordinary file, or cannot be read.
    """
    try:
        # The open file. Opening refuses a symlink and does not wait on a named pipe.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise BodyFileError(
            f"Body file {path} is missing. It may already have been sent; write a new one."
        ) from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise BodyFileError(
                f"Body file {path} must be an ordinary file."
            ) from error

        raise BodyFileError(f"Cannot read body file {path}: {error}") from error

    # The details of the file that was opened, whatever the path names by now.
    file_stat = os.fstat(descriptor)

    if not stat.S_ISREG(file_stat.st_mode):
        os.close(descriptor)
        raise BodyFileError(f"Body file {path} must be an ordinary file.")

    with os.fdopen(descriptor, "r", encoding="utf-8") as body_file:
        try:
            return BodyFileRead(body_file.read(), file_stat)
        except (OSError, UnicodeError) as error:
            raise BodyFileError(f"Cannot read body file {path}: {error}") from error


def _finish_body_file(path: Path, file_stat: os.stat_result) -> str | None:
    """Keep a changed body file or remove the delivered one, returning any warning.

    Args:
        path: The file path used for the delivered message.
        file_stat: The opened file's details from when it was read.

    Returns:
        A warning if the file changed or could not be removed, otherwise None.
    """
    if _body_file_changed(path, file_stat):
        return (
            "Sent, but the file changed after it was read, so it was kept. "
            "Its current contents have not been sent."
        )

    return _remove_body_file(path)


def _body_file_changed(path: Path, file_stat: os.stat_result) -> bool:
    """Check whether the path still names the file and contents that were read.

    Args:
        path: The file path used for the delivered message.
        file_stat: The opened file's details from when it was read.

    Returns:
        True if the file's identity, size or modified time differs, or inspecting the
        path fails for a reason other than the file being missing.
    """
    try:
        # The details of whatever the path names now, without following a symlink.
        current_stat = path.lstat()
    except FileNotFoundError:
        # A missing file reaches removal, which warns the sender not to send it again.
        return False
    except OSError:
        return True

    return (
        current_stat.st_dev,
        current_stat.st_ino,
        current_stat.st_size,
        current_stat.st_mtime_ns,
    ) != (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _remove_body_file(path: Path) -> str | None:
    """Remove a body file after its message has been delivered.

    A failed removal is returned as a warning, because the send has already succeeded
    and an error could lead the sender to send the same message again.

    Args:
        path: The body file that HCOM confirmed was delivered.

    Returns:
        None when the file was removed, otherwise a warning for the sender.
    """
    try:
        path.unlink()
    except OSError as error:
        return f"Sent, but could not remove body file {path}: {error}. Do not send it again."

    return None


class _ArgumentParser(argparse.ArgumentParser):
    """An argument parser that reports invalid arguments as a JSON refusal with --json."""

    def __init__(self, *, json_output: bool, **kwargs):
        """Store whether --json was passed, so a parsing error can still answer in JSON."""
        super().__init__(**kwargs)
        self.json_output = json_output

    def error(self, message: str) -> NoReturn:
        """Print a JSON refusal with --json, or argparse's usage message otherwise, then exit 2."""
        if self.json_output:
            raise SystemExit(_refuse(message, True, exit_code=2))

        super().error(message)


def _refuse(reason: str, json_output: bool, *, exit_code: int = 1) -> int:
    """Print a refusal as JSON or on stderr and return its exit code."""
    if json_output:
        print(json.dumps({"ok": False, "error": reason}))
    else:
        print(f"team-send: {reason}", file=sys.stderr)

    return exit_code


def _one_line(message: str) -> str:
    """Join multi-line HCOM error text into one line for the refusal message."""
    return " ".join(message.split())


if __name__ == "__main__":
    raise SystemExit(main())
