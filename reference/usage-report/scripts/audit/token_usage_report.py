#!/usr/bin/env python3
"""Write a bounded Claude and Codex token-usage report.

Codex ``event_msg`` records store token counts under ``payload.info``. An
empirical check of a real session with ten consecutive records confirmed that
``total_token_usage`` is cumulative per session, while ``last_token_usage`` is
the per-event delta. This script sums the latter and derives a delta from the
former only when the per-event value is absent.

All values produced by this script are tokens, not cost. The script reads
transcripts and the optional hcom database, then overwrites three fixed report
paths under the repository's ``.agent/audits/usage`` directory.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

# The repository root used to locate fixed report output paths.
REPO_ROOT = Path(__file__).resolve().parents[2]
# The audit script directory added to the import path for direct execution.
AUDIT_DIRECTORY = Path(__file__).resolve().parent

# Both direct script execution and ``from scripts.audit import token_usage_report`` are
# supported; each invocation style needs the sibling audit modules on sys.path.
if str(AUDIT_DIRECTORY) not in sys.path:
	sys.path.insert(0, str(AUDIT_DIRECTORY))

from tool_call_attribution import DRIVER_METHOD  # noqa: E402
from token_usage_parsing import (  # noqa: E402, F401
	CLAUDE_RECORD_TYPES,
	add_totals,
	empty_totals,
	number,
	parse_claude_session,
	parse_codex_session,
	records,
	usage_totals,
)
from token_usage_rendering import (  # noqa: E402
	render_detail_markdown,
	render_markdown,
)
from token_usage_types import (  # noqa: E402
	AggregateRow,
	DriverLedgerRow,
	DriverReconciliation,
	Group,
	HcomLabel,
	RatioData,
	Report,
	Session,
	SessionReport,
	TokenTotals,
	TOOLS,
	Window,
)

# The Claude project transcript folder, including any configured location.
CLAUDE_ROOT = (
	Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "projects"
)
# The Codex home directory used to find current and archived sessions.
_CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
# The current and archived Codex transcript folders searched for reports.
CODEX_ROOTS = (
	_CODEX_HOME / "sessions",
	_CODEX_HOME / "archived_sessions",
)
# The optional hcom database used to label transcript sessions.
HCOM_DATABASE = Path("~/.hcom/hcom.db").expanduser()
# The repository folder where the three latest report files are written.
REPORT_DIRECTORY = REPO_ROOT / ".agent/audits/usage"


def parse_date(value: str) -> datetime.date:
	"""Parse a command-line date as a UTC calendar date.

	Args:
		value: The YYYY-MM-DD value supplied by the caller.

	Returns:
		The parsed calendar date.
	"""
	try:
		return datetime.date.fromisoformat(value)
	except ValueError as error:
		raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def build_window(arguments: argparse.Namespace) -> Window:
	"""Build the half-open UTC window selected by the command line.

	Args:
		arguments: Parsed command-line options containing the date bounds.

	Returns:
		The UTC start, exclusive end, and display bounds for the report.
	"""
	if arguments.days is not None and (arguments.since or arguments.until):
		arguments.parser.error("use --days or --since/--until, not both")

	if bool(arguments.since) != bool(arguments.until):
		arguments.parser.error("--since and --until must be provided together")

	if arguments.since and arguments.until:
		if arguments.until < arguments.since:
			arguments.parser.error("--until must not be before --since")

		# The inclusive UTC start of the requested calendar-date range.
		start = datetime.datetime.combine(
			arguments.since,
			datetime.time.min,
			datetime.timezone.utc,
		)
		# The exclusive UTC end after the requested final date.
		end = datetime.datetime.combine(
			arguments.until + datetime.timedelta(days=1),
			datetime.time.min,
			datetime.timezone.utc,
		)
		return start, end, arguments.since.isoformat(), arguments.until.isoformat()

	# The number of trailing days selected, defaulting to seven.
	days = 7 if arguments.days is None else arguments.days
	if days < 0:
		arguments.parser.error("--days must not be negative")

	end = datetime.datetime.now(datetime.timezone.utc)
	start = end - datetime.timedelta(days=days)
	return (
		start,
		end,
		start.isoformat(timespec="seconds"),
		end.isoformat(timespec="seconds"),
	)


def normalise_path(value: object) -> str:
	"""Return a comparable absolute path for an optional hcom value.

	Args:
		value: Optional path value read from hcom.

	Returns:
		A comparable absolute path, or an empty string when absent.
	"""
	if not isinstance(value, str) or not value:
		return ""

	try:
		return str(Path(value).expanduser().resolve())
	except OSError:
		return os.path.abspath(os.path.expanduser(value))


def hcom_role(tag: object, parent_name: object) -> str:
	"""Extract the repository-scoped role suffix from an hcom label.

	Args:
		tag: Optional hcom tag.
		parent_name: Optional parent agent name used as fallback.

	Returns:
		The repository-scoped role suffix, or ``unattributed`` when absent.
	"""
	# The available hcom tag or parent name that may contain a team role.
	value = tag or parent_name or ""
	if not isinstance(value, str) or not value:
		return "unattributed"

	return value.rsplit("-", 1)[-1] or "unattributed"


def load_hcom_labels() -> tuple[dict[str, HcomLabel], dict[str, HcomLabel]]:
	"""Load best-effort transcript labels from hcom's read-only database.

	Returns:
		Mappings keyed by session id and normalised transcript path.
	"""
	# The hcom labels indexed by transcript session identifier.
	by_session_id: dict[str, HcomLabel] = {}
	# The hcom labels indexed by normalised transcript path.
	by_path: dict[str, HcomLabel] = {}

	if not HCOM_DATABASE.is_file():
		return by_session_id, by_path

	try:
		with contextlib.closing(
			sqlite3.connect(
				f"file:{HCOM_DATABASE}?mode=ro",
				uri=True,
				timeout=1,
			)
		) as connection:
			connection.row_factory = sqlite3.Row
			# The database records containing agent labels and transcript links.
			rows = connection.execute(
				"SELECT name, session_id, transcript_path, tag, parent_name, tool "
				"FROM instances"
			).fetchall()
	except sqlite3.Error:
		return by_session_id, by_path

	for row in rows:
		# The agent name, role, and runtime for this database record.
		label: HcomLabel = {
			"agent_name": row["name"] or "unattributed",
			"role": hcom_role(row["tag"], row["parent_name"]),
			"tool": row["tool"] or "unknown",
		}
		# The session identifier used to join this agent label to a transcript.
		session_id = row["session_id"]
		if isinstance(session_id, str) and session_id:
			by_session_id[session_id] = label

		# The normalised transcript path used to join this agent label.
		path = normalise_path(row["transcript_path"])
		if path:
			by_path[path] = label

	return by_session_id, by_path


def apply_hcom_label(
	session: Session,
	by_session_id: dict[str, HcomLabel],
	by_path: dict[str, HcomLabel],
) -> None:
	"""Join one parsed session to hcom, retaining an explicit fallback label.

	Args:
		session: Parsed session receiving its hcom label.
		by_session_id: Labels indexed by transcript session id.
		by_path: Labels indexed by normalised transcript path.

	Returns:
		None. The session hcom label is updated in place when matched.
	"""
	# The hcom label matched by transcript path or, failing that, session identifier.
	label = by_path.get(normalise_path(session["transcript_path"]))
	if label is None:
		label = by_session_id.get(session["session_id"])

	if label is not None:
		session["hcom"] = dict(label)


def make_report(sessions: list[Session], window: Window) -> Report:
	"""Build the deterministic JSON report object.

	Args:
		sessions: Parsed sessions included in the selected window.
		window: UTC window and display bounds for the report.
	Returns:
		The deterministic serialisable report object.
	"""
	# The UTC filter bounds and dates displayed for the selected window.
	start, end, display_since, display_until = window

	def ratio_data(numerator: int, denominator: int) -> RatioData:
		"""Return ratio inputs as well as the calculated ratio for JSON consumers.

		Args:
			numerator: Ratio numerator in tokens.
			denominator: Ratio denominator in tokens.

		Returns:
			A serialisable ratio object containing both inputs and the result.
		"""
		return {
			"numerator_tokens": numerator,
			"denominator_tokens": denominator,
			"ratio": None if not denominator else round(numerator / denominator, 6),
		}

	def add_group(
		group: Group,
		key: str,
		tool: str,
		session_count: int,
		tokens: TokenTotals,
	) -> None:
		"""Add a session or event aggregate to a grouped report section.

		Args:
			group: Group receiving the aggregate.
			key: Grouping key for the aggregate.
			tool: Runtime name for the aggregate.
			session_count: Number of sessions represented by the aggregate.
			tokens: Normalised token totals to add.

		Returns:
			None. The grouped aggregate is updated in place.
		"""
		# The aggregate rows already collected for this grouping key.
		row = group.setdefault(key, {})
		# The aggregate row for this runtime within the grouping key.
		tool_row = row.setdefault(
			tool,
			{
				"session_count": 0,
				"tokens": empty_totals(tool),
			},
		)
		tool_row["session_count"] += session_count
		add_totals(tool_row["tokens"], tokens, tool)

	# The token totals and session counts collected for each runtime.
	by_tool: dict[str, AggregateRow] = {
		tool: {
			"session_count": 0,
			"tokens": empty_totals(tool),
		}
		for tool in TOOLS
	}
	# The token totals grouped by model within each runtime.
	by_model: dict[str, Group] = {tool: {} for tool in TOOLS}
	# The token totals grouped by UTC day.
	by_day: Group = {}
	# The token totals grouped by project directory.
	by_project: Group = {}
	# The token totals grouped by hcom role.
	by_role: Group = {}

	for session in sessions:
		# The runtime used to place this session in the correct totals.
		tool = session["tool"]
		by_tool[tool]["session_count"] += 1
		add_totals(by_tool[tool]["tokens"], session["tokens"], tool)

		for model, tokens in session["models"].items():
			add_group(by_model[tool], model, tool, 1, tokens)

		for day, tokens in session["days"].items():
			add_group(by_day, day, tool, 1, tokens)

		add_group(by_project, session["project_directory"], tool, 1, session["tokens"])
		add_group(by_role, session["hcom"]["role"], tool, 1, session["tokens"])

	for tool in TOOLS:
		# The accumulated token fields used to calculate this runtime's ratios.
		tokens = by_tool[tool]["tokens"]
		by_tool[tool]["empty"] = by_tool[tool]["session_count"] == 0
		if tool == "Claude":
			by_tool[tool]["cache_read_ratio"] = ratio_data(
				tokens.get("cache_read_input_tokens", 0),
				tokens.get("total_input_tokens", 0),
			)
		else:
			by_tool[tool]["reasoning_output_ratio"] = ratio_data(
				tokens.get("reasoning_output_tokens", 0),
				tokens.get("output_tokens", 0),
			)

		if by_tool[tool]["empty"]:
			by_tool[tool]["message"] = (
				f"No {tool} token usage records in the selected window."
			)

	# The combined driver rows indexed by category and key.
	rows: dict[tuple[str, str], DriverLedgerRow] = {}
	# The number of tool calls across all included sessions.
	tool_call_count = 0
	# The number of tool calls without a driver grouping across sessions.
	unattributed_count = 0
	# The estimated payload tokens for calls without a driver grouping.
	unattributed_payload = 0

	for session in sessions:
		tool_call_count += session["tool_call_count"]
		unattributed_count += session["unattributed_count"]
		unattributed_payload += session["unattributed"]["payload_estimate_tokens"]

		for source in session["driver_ledger"]:
			# The category and key that identify a driver row across sessions.
			identity = (source["category"], source["key"])
			if identity not in rows:
				rows[identity] = dict(source)
			else:
				# The existing combined driver row updated with this session's counts.
				row = rows[identity]
				row["count"] += source["count"]
				row["payload_estimate_tokens"] += source["payload_estimate_tokens"]
				row["failure_count"] += source["failure_count"]
				row["retry_count"] += source["retry_count"]
				row["repeated"] = row["repeated"] or source["repeated"]

	# The driver rows ordered by estimated payload, then by category and key.
	ordered_rows = sorted(
		rows.values(),
		key=lambda row: (-row["payload_estimate_tokens"], row["category"], row["key"]),
	)
	# The number of tool calls assigned to the ordered driver rows.
	attributed_count = sum(row["count"] for row in ordered_rows)
	# The attribution totals used to check that all calls are accounted for.
	driver_data: DriverReconciliation = {
		"driver_ledger": ordered_rows,
		"tool_call_count": tool_call_count,
		"attributed_count": attributed_count,
		"unattributed_count": unattributed_count,
		"unattributed": {
			"count": unattributed_count,
			"payload_estimate_tokens": unattributed_payload,
			"method": DRIVER_METHOD,
		},
		"reconciles": tool_call_count == attributed_count + unattributed_count,
	}
	# The sessions ranked by token total, with ties broken by runtime, then session ID.
	ordered_sessions = sorted(
		sessions,
		key=lambda session: (
			-session["tokens"]["total_tokens"],
			session["tool"],
			session["session_id"],
		),
	)

	def session_report(session: Session, rank: int) -> SessionReport:
		"""Build one serialised session row.

		Args:
			session: Parsed session to serialise.
			rank: One-based ranking for the session.

		Returns:
			The serialised session row.
		"""
		return {
			"rank": rank,
			"tool": session["tool"],
			"session_id": session["session_id"],
			"transcript_path": session["transcript_path"],
			"project_directory": session["project_directory"],
			"hcom": session["hcom"],
			"models": sorted(session["models"]),
			"tokens": session["tokens"],
			"tool_call_count": session["tool_call_count"],
			"skipped_record_count": session["skipped_record_count"],
			"unattributed_count": session["unattributed_count"],
			"unattributed": session["unattributed"],
			"driver_reconciles": session["driver_reconciles"],
			"driver_ledger": session["driver_ledger"],
		}

	# The serialised rows for every included session in ranked order.
	all_sessions: list[SessionReport] = [
		session_report(session, rank)
		for rank, session in enumerate(ordered_sessions, start=1)
	]
	# The ten highest-token session rows shown in the compact report.
	top_sessions = all_sessions[:10]

	# The runtime totals with empty token objects omitted from the report.
	totals_by_tool: dict[str, AggregateRow] = {
		tool: {
			field: value
			for field, value in row.items()
			if not (row["empty"] and field == "tokens")
		}
		for tool, row in by_tool.items()
	}

	return {
		"units": "tokens, not cost",
		"window": {
			"since": display_since,
			"until": display_until,
			"start_utc": start.isoformat(),
			"end_utc_exclusive": end.isoformat(),
		},
		"empty_window": not sessions,
		"partial_data": {
			"partial": any(session["skipped_record_count"] > 0 for session in sessions),
			"skipped_record_count": sum(
				session["skipped_record_count"] for session in sessions
			),
			"skipped_record_counts": {
				tool: sum(
					session["skipped_record_count"]
					for session in sessions
					if session["tool"] == tool
				)
				for tool in TOOLS
			},
		},
		"session_count": len(sessions),
		"totals_by_tool": totals_by_tool,
		"totals_by_model": by_model,
		"totals_by_day": by_day,
		"totals_by_project": by_project,
		"totals_by_role": by_role,
		"driver_ledger": driver_data["driver_ledger"],
		"driver_reconciliation": driver_data,
		"sessions": all_sessions,
		"top_sessions": top_sessions,
	}


def parse_arguments() -> argparse.Namespace:
	"""Parse command-line bounds and retain the parser for validation errors.

	Returns:
		Parsed command-line options with the parser attached for validation errors.
	"""
	# The command-line parser that reports invalid date selections.
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--since", type=parse_date, help="inclusive UTC date, YYYY-MM-DD"
	)
	parser.add_argument(
		"--until", type=parse_date, help="inclusive UTC date, YYYY-MM-DD"
	)
	parser.add_argument(
		"--days", type=int, help="window ending now in UTC (default: 7)"
	)
	# The parsed command-line options, which also carry the parser so later checks can report errors.
	arguments = parser.parse_args()
	arguments.parser = parser
	return arguments


def transcript_paths() -> tuple[list[Path], list[Path]]:
	"""Return sorted Claude and Codex transcript paths that currently exist.

	Returns:
		Sorted Claude paths followed by sorted Codex rollout paths.
	"""
	# The Claude transcript files found under the configured projects folder.
	claude_paths = sorted(CLAUDE_ROOT.rglob("*.jsonl")) if CLAUDE_ROOT.is_dir() else []
	# The Codex transcript files found across current and archived sessions.
	codex_paths = []
	# The Codex transcript paths already collected from the search roots.
	seen = set()

	for root in CODEX_ROOTS:
		if not root.is_dir():
			continue

		for path in root.rglob("rollout-*.jsonl"):
			if path not in seen:
				seen.add(path)
				codex_paths.append(path)

	return claude_paths, sorted(codex_paths)


def write_report(report: Report) -> tuple[Path, Path, Path]:
	"""Overwrite the fixed summary, detail, and JSON report paths.

	Args:
		report: Serialised report to write.

	Returns:
		Summary Markdown path, detail Markdown path, and JSON path.
	"""
	REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
	# The fixed JSON path for the latest usage report.
	json_path = REPORT_DIRECTORY / "latest.json"
	# The fixed Markdown path for the compact usage summary.
	markdown_path = REPORT_DIRECTORY / "latest.md"
	# The fixed Markdown path for the detailed usage report.
	detail_markdown_path = REPORT_DIRECTORY / "latest-detail.md"

	json_path.write_text(
		json.dumps(report, indent=2, sort_keys=True) + "\n",
		encoding="utf-8",
	)
	markdown_path.write_text(render_markdown(report), encoding="utf-8")
	detail_markdown_path.write_text(render_detail_markdown(report), encoding="utf-8")

	return markdown_path, detail_markdown_path, json_path


def main() -> None:
	"""Read transcripts, write both reports, and print only a bounded summary.

	Returns:
		None. Reports are written and a bounded summary is printed.
	"""
	# The selected command-line bounds for this report run.
	arguments = parse_arguments()
	# The UTC filter bounds and display dates for this report run.
	window = build_window(arguments)
	# The UTC instants used to filter transcript records.
	start, end, _, _ = window
	# The Claude and Codex transcript paths available for this report run.
	claude_paths, codex_paths = transcript_paths()
	# The parsed sessions included in the selected window.
	sessions: list[Session] = []

	for path in claude_paths:
		# The Claude session parsed from this transcript, when it has report data.
		session = parse_claude_session(path, start, end)
		if session is not None:
			sessions.append(session)

	for path in codex_paths:
		session = parse_codex_session(path, start, end)
		if session is not None:
			sessions.append(session)

	# The agent labels indexed by session identifier and transcript path.
	by_session_id, by_path = load_hcom_labels()
	for session in sessions:
		apply_hcom_label(session, by_session_id, by_path)

	# The serialisable report assembled from parsed sessions.
	report = make_report(sessions, window)
	# The three report paths written for this run, in display order.
	markdown_path, detail_markdown_path, json_path = write_report(report)
	print(
		f"Wrote {report['session_count']} sessions, tokens not cost, "
		f"to {markdown_path}, {detail_markdown_path}, and {json_path}"
	)


if __name__ == "__main__":
	main()
