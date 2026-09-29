"""Command-line entry point for capturing worktree review feedback."""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from prompt_toolkit import prompt

from review_feedback import style
from review_feedback.clipboard import (
	ClipboardError,
	copy_to_clipboard,
	read_clipboard,
	strip_clipboard_gutter,
)
from review_feedback.draft import (
	Draft,
	DraftEntry,
	DraftError,
	DraftValidationError,
	DraftStore,
	NoActiveDraftError,
	ValidationReport,
	append_entry,
	load_active,
	location_text,
	remove_entry,
	repository_fingerprint,
	resolve_store,
	retire_active,
	save_active,
	validation_notice_text,
	validate_entries,
)
from review_feedback.git_selection import SelectionError, resolve_selection
from review_feedback.render import render_packet


class CliError(RuntimeError):
	"""Report an interactive command failure with a recovery action."""


def build_parser() -> argparse.ArgumentParser:
	"""Build the review-feedback command parser."""
	parser = argparse.ArgumentParser(
		prog="review-feedback",
		description=(
			"Capture copied Git worktree selections and comments as review feedback."
		),
	)
	commands = parser.add_subparsers(dest="command", required=True)

	commands.add_parser(
		"add",
		help="capture the current macOS clipboard selection and one comment",
	)

	commands.add_parser(
		"show",
		help="display entries in the active review draft",
	)

	remove_parser = commands.add_parser(
		"remove",
		help="remove one entry without renumbering the remaining entries",
	)
	remove_parser.add_argument(
		"number", type=int, help="Stable entry number to remove."
	)

	preview_parser = commands.add_parser(
		"preview",
		help="validate the active draft and write its packet",
	)
	preview_parser.add_argument(
		"--copy",
		action="store_true",
		help="also copy the packet to the macOS clipboard",
	)

	finish_parser = commands.add_parser(
		"finish",
		help="write the packet and retire the active draft to Trash",
	)
	finish_parser.add_argument(
		"--copy",
		action="store_true",
		help="also copy the packet to the macOS clipboard",
	)

	commands.add_parser(
		"clear",
		help="retire the active draft to Trash without writing a packet",
	)

	return parser


def main(argv: list[str] | None = None) -> int:
	"""Run a review-feedback command and report recoverable failures."""
	args = build_parser().parse_args(argv)

	try:
		return _run_command(args)
	except (ClipboardError, CliError, DraftError, SelectionError) as error:
		sys.stderr.write(style.status("failed", "Error", str(error)) + "\n")
		return 2


def _run_command(args: argparse.Namespace) -> int:
	"""Dispatch parsed arguments to one review-feedback command."""
	if args.command == "add":
		return _add()
	if args.command == "show":
		return _show()
	if args.command == "remove":
		return _remove(args.number)
	if args.command == "preview":
		return _preview(args.copy)
	if args.command == "finish":
		return _finish(args.copy)
	if args.command == "clear":
		return _clear()

	raise CliError(f"unknown command: {args.command}")


def _add() -> int:
	"""Capture clipboard text, resolve its locations, and append entries."""
	store = resolve_store()
	selection = read_clipboard()
	selection = strip_clipboard_gutter(selection)
	locations = resolve_selection(selection, store.root)
	comment = _prompt_comment()
	fingerprint = repository_fingerprint(store.root)
	draft = load_active(store) or Draft([])
	entries: list[DraftEntry] = []
	for location in locations:
		entries.append(append_entry(draft, location, selection, comment, fingerprint))

	save_active(store, draft)

	for entry in entries:
		sys.stdout.write(
			style.status(
				"success",
				"Added",
				f"entry {entry.number} for {location_text(entry)}",
			)
			+ "\n"
		)

	return 0


def _show() -> int:
	"""Display active entries without reading or displaying source excerpts."""
	store = resolve_store()
	draft = load_active(store)

	if draft is None:
		sys.stdout.write(style.status("info", "No active draft") + "\n")
		return 0

	sys.stdout.write(
		style.status("success", "Active draft", f"{len(draft.entries)} entries") + "\n"
	)
	report = validate_entries(store, draft, strict=False)
	_write_validation_notices(report, sys.stdout, include_missing=True)

	for entry in draft.entries:
		sys.stdout.write(
			style.row(
				f"Entry {entry.number}",
				f"{location_text(entry)}: {entry.comment}",
			)
			+ "\n"
		)

	return 0


def _remove(number: int) -> int:
	"""Remove one stable-numbered entry from the active draft."""
	store = resolve_store()
	draft = _require_active(store)
	removed = remove_entry(draft, number)
	save_active(store, draft)
	sys.stdout.write(
		style.status("success", "Removed", f"entry {removed.number}") + "\n"
	)
	return 0


def _preview(copy_packet: bool) -> int:
	"""Validate and write the active packet without changing draft state."""
	store = resolve_store()
	draft = _require_active(store)
	_validate_packet(store, draft)
	packet = render_packet(draft)
	_write_packet(packet, copy_packet)
	return 0


def _finish(copy_packet: bool) -> int:
	"""Validate, write, and retire the active packet after output succeeds."""
	store = resolve_store()
	draft = _require_active(store)
	_validate_packet(store, draft)
	packet = render_packet(draft)
	_write_packet(packet, copy_packet)
	retire_active(store, "finished")
	return 0


def _clear() -> int:
	"""Retire an abandoned active draft without producing a packet."""
	store = resolve_store()
	retired_path = retire_active(store, "abandoned")
	sys.stdout.write(
		style.status("success", "Cleared", f"draft moved to {retired_path}") + "\n"
	)
	return 0


def _prompt_comment() -> str:
	"""Prompt for one non-empty review comment."""
	try:
		if sys.stdin.isatty():
			comment = prompt("Comment: ")
		else:
			comment = input("Comment: ")
	except EOFError as error:
		raise CliError(
			"comment prompt closed; run `review-feedback add` and enter one comment"
		) from error

	if not comment.strip():
		raise CliError("comment cannot be empty; run `review-feedback add` again")

	return comment


def _require_active(store: DraftStore) -> Draft:
	"""Load an active draft or report the command needed to create one."""
	draft = load_active(store)
	if draft is None:
		raise NoActiveDraftError(
			"no active review draft exists; run `review-feedback add` first"
		)

	return draft


def _validate_packet(store: DraftStore, draft: Draft) -> None:
	"""Report ambiguous entries and raise if any entry is genuinely missing."""
	report = validate_entries(store, draft, strict=False)
	_write_validation_notices(report, sys.stderr, include_missing=False)

	if report.missing:
		raise DraftValidationError(report.missing)


def _write_validation_notices(
	report: ValidationReport,
	stream: TextIO,
	*,
	include_missing: bool,
) -> None:
	"""Write one warning line per notice; skip missing-entry notices unless requested."""
	for notice in report.notices:
		if notice.missing and not include_missing:
			continue

		stream.write(
			style.status(
				"warning",
				f"Entry {notice.entry_number}",
				validation_notice_text(notice),
			)
			+ "\n"
		)


def _write_packet(packet: str, copy_packet: bool) -> None:
	"""Write a packet and optionally copy it before any draft retirement."""
	try:
		sys.stdout.write(packet)
		sys.stdout.flush()
	except (BrokenPipeError, OSError) as error:
		raise CliError(f"could not write packet: {error}") from error

	if copy_packet:
		copy_to_clipboard(packet)


if __name__ == "__main__":
	raise SystemExit(main())
