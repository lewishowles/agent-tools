"""Command-line entry point for agent-run."""

import argparse
import sys
from collections.abc import Sequence
from typing import NoReturn

from agent_run.output import render_error, render_success


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports mistakes in the same format as other errors.

    Plain argparse prints its own message and exits, which would break the JSON
    envelope when `--json` is set.
    """

    def parse_args(
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        """Parse arguments, first noting whether `--json` was given.

        The check runs before parsing because argparse calls `error` part way
        through, before the parsed `--json` value exists.
        """
        values = list(sys.argv[1:] if args is None else args)
        self._json_mode = "--json" in values

        return super().parse_args(values, namespace)

    def error(self, message: str) -> NoReturn:
        """Report a usage error and exit with the `usage` status.

        Args:
            message: argparse's description of what was wrong.
        """
        diagnostic = f"{self.format_usage().strip()}\n{self.prog}: error: {message}"
        exit_code = render_error(
            json_mode=self._json_mode,
            code="usage",
            message=message,
            text=diagnostic,
            diagnostic=diagnostic,
        )
        raise SystemExit(exit_code)


def main(argv: Sequence[str] | None = None) -> int:
    """Run agent-run and return its exit status.

    No commands exist yet, so every valid call shows help. Invalid arguments
    exit through `_ArgumentParser.error` instead of returning.

    Args:
        argv: Arguments without the program name; defaults to `sys.argv[1:]`.
    """
    parser = _ArgumentParser(
        prog="agent-run",
        description="Run project commands with bounded evidence.",
        add_help=False,
    )
    parser.add_argument(
        "--help",
        "-h",
        action="store_true",
        help="Show this help message and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write one structured JSON result to standard output.",
    )

    parsed = parser.parse_args(argv)
    help_text = parser.format_help()

    if parsed.json:
        return render_success(json_mode=True, data={"help": help_text})

    return render_success(json_mode=False, text=help_text)


if __name__ == "__main__":
    raise SystemExit(main())
