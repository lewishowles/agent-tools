"""Send an HCOM message to one live teammate in the sender's team."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import NoReturn

# The team roles a sender can hold and send to. The last part of every team tag
# is one of these.
ROLES = frozenset({"orchestrator", "scout", "implementer", "reviewer"})

# The HCOM statuses of an agent that can still receive a message. Inactive and
# unknown agents never count as a match.
LIVE_STATUSES = frozenset({"active", "listening", "blocked"})


def main(argv: list[str] | None = None) -> int:
    """Send the piped message to the one live teammate for the requested role and return 0.

    A refusal or failed send prints the reason and returns 1, and a message is never sent twice.
    Invalid arguments exit with status 2, as argparse does.
    """
    arguments = sys.argv[1:] if argv is None else argv
    parser = _ArgumentParser(
        json_output="--json" in arguments,
        # Only the exact --json flag switches output to JSON, including for usage errors.
        allow_abbrev=False,
        description="Send to one live teammate for a role in your HCOM team.",
    )
    parser.add_argument("role", help="orchestrator, scout, implementer, or reviewer")
    parser.add_argument("--intent", required=True, choices=("request", "inform", "ack"))
    parser.add_argument("--reply-to", help="HCOM event ID to reply to")
    parser.add_argument("--thread", help="HCOM thread name")
    parser.add_argument("--json", action="store_true", help="Print structured output")
    args = parser.parse_args(arguments)

    if args.role not in ROLES:
        return _refuse(
            f"Unknown role: {args.role}. "
            "Choose orchestrator, scout, implementer, or reviewer.",
            args.json,
        )

    sender_tag = os.environ.get("HCOM_TAG")
    sender_name = os.environ.get("HCOM_NAME")

    if not sender_tag:
        return _refuse("HCOM_TAG is unset.", args.json)

    if not sender_name:
        return _refuse("HCOM_NAME is unset.", args.json)

    prefix, _, sender_role = sender_tag.rpartition("-")

    if not prefix or sender_role not in ROLES:
        return _refuse("HCOM_TAG must end in a team role.", args.json)

    if args.role == sender_role:
        return _refuse(f"Cannot send to your own role: {args.role}.", args.json)

    target_tag = f"{prefix}-{args.role}"

    if args.intent == "ack" and not args.reply_to:
        return _refuse("--intent ack requires --reply-to.", args.json)

    if sys.stdin.isatty():
        return _refuse("Pipe a message body to stdin.", args.json)

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
        return _refuse(f"Unexpected HCOM send receipt: {detail}", args.json)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "recipient": recipient["name"],
                        "base_name": recipient["base_name"],
                        "tag": target_tag,
                        "event_id": event_id,
                    },
                }
            )
        )
    else:
        print(f"Sent to {recipient['name']} (event {event_id}).")

    return 0


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
