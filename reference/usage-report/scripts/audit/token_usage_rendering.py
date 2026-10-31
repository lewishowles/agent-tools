"""Render typed usage reports as Markdown."""

from __future__ import annotations

from typing import Literal, TypedDict

from token_usage_types import (
	DriverLedgerRow,
	Group,
	RatioData,
	Report,
	SessionReport,
	TOOLS,
	Unattributed,
)


class SessionGroups(TypedDict):
	"""Group sessions by the activity shown in each report view."""

	# The sessions with tool calls that receive driver rows in the report.
	visible: list[SessionReport]
	# The sessions with tokens but no tool calls that receive a short note.
	no_driver: list[SessionReport]
	# The sessions with neither tokens nor calls that are omitted from rows.
	zero_activity: list[SessionReport]


# Identifies how one session is rendered in the report.
SessionCategory = Literal["visible", "no_driver", "zero_activity"]


def display_token_count(tokens: int) -> str:
	"""Format a token count for the Markdown report.

	Args:
		tokens: Token count to format.

	Returns:
		The count with thousands separators.
	"""
	return f"{tokens:,}"


def display_path(value: object) -> str:
	"""Keep grouped paths safe in a Markdown table.

	Args:
		value: Path or other value to display.

	Returns:
		A string with Markdown table separators escaped.
	"""
	return str(value).replace("|", "\\|").replace("\n", " ")


def classify_session(session: SessionReport) -> SessionCategory:
	"""Return the rendering category for one session.

	Args:
		session: Session row selected for the report window.

	Returns:
		The category used by the compact and detail renderers.
	"""
	if session["tool_call_count"] > 0:
		return "visible"

	if session["tokens"]["total_tokens"] > 0:
		return "no_driver"

	return "zero_activity"


def classify_sessions(sessions: list[SessionReport]) -> SessionGroups:
	"""Split sessions into rendered, no-driver, and omitted groups.

	Args:
		sessions: Session rows selected for the report window.

	Returns:
		Sessions with tool calls, sessions with tokens but no tool calls, and
		zero-activity sessions whose rows are omitted.
	"""
	# The sessions whose tool calls will appear in driver tables.
	visible: list[SessionReport] = []
	# The sessions with token usage but no tool calls at all.
	no_driver: list[SessionReport] = []
	# The sessions omitted because they have no usage or tool calls.
	zero_activity: list[SessionReport] = []

	for session in sessions:
		# The report view chosen for this session's activity.
		category = classify_session(session)
		if category == "visible":
			visible.append(session)
		elif category == "no_driver":
			no_driver.append(session)
		else:
			zero_activity.append(session)

	return {
		"visible": visible,
		"no_driver": no_driver,
		"zero_activity": zero_activity,
	}


def zero_activity_note_lines(session_groups: SessionGroups) -> list[str]:
	"""Render the shared note for omitted zero-activity sessions.

	Args:
		session_groups: Sessions grouped by rendered activity.

	Returns:
		A Markdown note, or no lines when no session was omitted.
	"""
	# The number of sessions omitted because they have no activity.
	zero_activity_count = len(session_groups["zero_activity"])
	if not zero_activity_count:
		return []

	# The singular or plural noun used in the omission note.
	label = "session" if zero_activity_count == 1 else "sessions"
	# The matching singular or plural verb used in the omission note.
	verb = "is" if zero_activity_count == 1 else "are"
	return [
		f"{zero_activity_count} {label} had no attributable activity in the selected window and {verb} omitted."
	]


def format_ratio(data: RatioData) -> str:
	"""Format a ratio row for Markdown.

	Args:
		data: Ratio object containing the calculated value.

	Returns:
		A percentage string, or ``n/a`` when no ratio is available.
	"""
	if data["ratio"] is None:
		return "n/a"

	return f"{data['ratio'] * 100:.2f}%"


def markdown_table_for_group(group: Group) -> list[str]:
	"""Render grouped token totals as a deterministic Markdown table.

	Args:
		group: Grouped token totals to render.

	Returns:
		Markdown table lines, or a no-data line when the group is empty.
	"""
	# The Markdown header and rows for grouped token totals.
	lines = [
		"| Group | Tool | Sessions | Total tokens |",
		"| --- | --- | ---: | ---: |",
	]

	for key in sorted(group):
		for tool in TOOLS:
			# The aggregate for this group and runtime, when present.
			row = group[key].get(tool)
			if row is None:
				continue

			lines.append(
				f"| {display_path(key)} | {tool} | {row['session_count']} | "
				f"{display_token_count(row['tokens']['total_tokens'])} |"
			)

	return lines if len(lines) > 2 else ["No data in the selected window."]


def markdown_driver_table(
	rows: list[DriverLedgerRow],
	unattributed: Unattributed,
) -> list[str]:
	"""Render ranked driver rows without including raw transcript payloads.

	Args:
		rows: Ranked attributed driver rows to render.
		unattributed: Aggregate row for calls without a classification.

	Returns:
		Markdown table lines, or a no-data line when no calls exist.
	"""
	# The Markdown header and rows for ranked driver totals.
	lines = [
		(
			"| Rank | Category | Key | Count | Payload estimate (tokens) | "
			"Method | Failures | Retries | Repeated |"
		),
		"| ---: | --- | --- | ---: | ---: | --- | ---: | ---: | --- |",
	]

	for rank, row in enumerate(rows, start=1):
		lines.append(
			f"| {rank} | {row['category']} | {display_path(row['key'])} | {row['count']} | "
			f"{row['payload_estimate_tokens']} | {row['method']} | {row['failure_count']} | "
			f"{row['retry_count']} | {'yes' if row['repeated'] else 'no'} |"
		)

	if unattributed["count"]:
		# The next rank after all attributed driver rows.
		rank = len(rows) + 1
		lines.append(
			f"| {rank} | unattributed | — | {unattributed['count']} | "
			f"{unattributed['payload_estimate_tokens']} | {unattributed['method']} | — | — | — |"
		)

	return lines if len(lines) > 2 else ["No data in the selected window."]


def render_window_section(report: Report) -> list[str]:
	"""Render the report window, empty-window, and partial-data sections.

	Args:
		report: Serialised report containing window and partial-data details.

	Returns:
		Markdown lines for the report window sections.
	"""
	# The selected dates and UTC bounds shown at the top of the report.
	window = report["window"]
	# The Markdown lines describing the report window and its session count.
	lines = [
		"## Window (tokens, not cost)",
		"",
		f"- Since (UTC): `{window['since']}`",
		f"- Until (UTC): `{window['until']}`",
		f"- Sessions with usage: `{report['session_count']}`",
		"",
	]

	if report["empty_window"]:
		lines.extend(
			[
				"## Empty window (tokens, not cost)",
				"",
				"No Claude or Codex usage records were found in the selected window.",
				"",
			]
		)

	# The skipped-record summary shown when transcript data is incomplete.
	partial_data = report["partial_data"]
	if partial_data["partial"]:
		lines.extend(
			[
				"## Partial data",
				"",
				f"Skipped records: **{partial_data['skipped_record_count']}**",
				"",
				"| Runtime | Skipped records |",
				"| --- | ---: |",
			]
		)
		for tool in TOOLS:
			lines.append(f"| {tool} | {partial_data['skipped_record_counts'][tool]} |")

	return lines


def render_tool_totals_section(report: Report) -> list[str]:
	"""Render token totals grouped by runtime tool.

	Args:
		report: Serialised report containing tool totals.

	Returns:
		Markdown lines for the tool totals section.
	"""
	# The aggregate totals keyed by runtime for this report.
	by_tool = report["totals_by_tool"]
	# The Markdown header and rows for runtime token totals.
	lines = [
		"## Totals by tool (tokens, not cost)",
		"",
		"| Tool | Sessions | Total tokens | Input tokens | Output tokens |",
		"| --- | ---: | ---: | ---: | ---: |",
	]

	for tool in TOOLS:
		# The current runtime's token totals and empty-data flag.
		row = by_tool[tool]
		if row["empty"]:
			lines.append(f"| {tool} | 0 | no data | no data | no data |")
			continue

		# The token fields used to fill this runtime's table row.
		tokens = row["tokens"]
		# The combined input count when available, otherwise the source input count.
		input_tokens = tokens.get("total_input_tokens", tokens.get("input_tokens", 0))
		lines.append(
			f"| {tool} | {row['session_count']} | "
			f"{display_token_count(tokens['total_tokens'])} | "
			f"{display_token_count(input_tokens)} | "
			f"{display_token_count(tokens['output_tokens'])} |"
		)

	return lines


def render_group_tables_section(report: Report) -> list[str]:
	"""Render model, day, project, and hcom role totals.

	Args:
		report: Serialised report containing grouped totals.

	Returns:
		Markdown lines for the grouped totals sections.
	"""
	# The Markdown lines for model, day, project, and role totals.
	lines = ["", "## Totals by model (tokens, not cost)", ""]
	for line in markdown_table_for_group(report["totals_by_model"]["Claude"]):
		lines.append(line)
	for line in markdown_table_for_group(report["totals_by_model"]["Codex"]):
		lines.append(line)

	lines.extend(["", "## Totals by day (tokens, not cost)", ""])
	lines.extend(markdown_table_for_group(report["totals_by_day"]))
	lines.extend(["", "## Totals by project directory (tokens, not cost)", ""])
	lines.extend(markdown_table_for_group(report["totals_by_project"]))
	lines.extend(["", "## Totals by hcom role (tokens, not cost)", ""])
	lines.extend(markdown_table_for_group(report["totals_by_role"]))

	return lines


def render_top_sessions_section(report: Report) -> list[str]:
	"""Render the top sessions ranked by total tokens.

	Args:
		report: Serialised report containing ranked sessions.

	Returns:
		Markdown lines for the top sessions section.
	"""
	# The Markdown header and rows for the highest-token sessions.
	lines = [
		"",
		"## Top 10 sessions by total tokens (tokens, not cost)",
		"",
		"| Rank | Tool | Session id | Total tokens | Project directory | Transcript path | Hcom role |",
		"| ---: | --- | --- | ---: | --- | --- | --- |",
	]

	if report["top_sessions"]:
		for session in report["top_sessions"]:
			lines.append(
				f"| {session['rank']} | {session['tool']} | `{session['session_id']}` | "
				f"{display_token_count(session['tokens']['total_tokens'])} | "
				f"{display_path(session['project_directory'])} | "
				f"`{display_path(session['transcript_path'])}` | "
				f"{display_path(session['hcom']['role'])} |"
			)
	else:
		lines.append("| | | no data | | | | |")

	return lines


def render_driver_views_section(report: Report) -> list[str]:
	"""Render aggregate and per-session driver ledger views.

	Args:
		report: Serialised report containing aggregate and session ledgers.

	Returns:
		Markdown lines for the driver ledger sections.
	"""
	# The Markdown lines for aggregate and per-session tool-call drivers.
	lines = [
		"",
		"## Driver ledger (ranked aggregate)",
		"",
		(
			f"Tool calls: **{report['driver_reconciliation']['tool_call_count']}** = "
			f"{report['driver_reconciliation']['attributed_count']} attributed + "
			f"{report['driver_reconciliation']['unattributed_count']} unattributed."
		),
		"",
	]
	lines.extend(
		markdown_driver_table(
			report["driver_ledger"],
			report["driver_reconciliation"]["unattributed"],
		)
	)

	lines.extend(["", "## Driver ledger by session", ""])
	# The sessions sorted into those with driver rows, those with only token usage, and those with no activity.
	session_groups = classify_sessions(report["sessions"])
	if report["sessions"]:
		for session in report["sessions"]:
			# The report view selected for this session's activity.
			category = classify_session(session)
			if category == "zero_activity":
				continue

			if category == "no_driver":
				# The token count shown for a session without tool calls.
				total_tokens = session["tokens"]["total_tokens"]
				lines.extend(
					[
						f"{session['tool']} `{session['session_id']}`: "
						f"{display_token_count(total_tokens)} tokens, "
						"no attributed tool-call driver.",
						"",
					]
				)
				continue

			# The ranked driver rows shown under this session's heading.
			driver_table = markdown_driver_table(
				session["driver_ledger"], session["unattributed"]
			)
			lines.extend(
				[
					f"### {session['tool']} `{session['session_id']}`",
					"",
					(
						f"Tool calls: **{session['tool_call_count']}** = "
						f"{session['tool_call_count'] - session['unattributed_count']} attributed + "
						f"{session['unattributed_count']} unattributed."
					),
					"",
				]
			)
			lines.extend(driver_table)
			lines.append("")
	else:
		lines.append("No data in the selected window.")

	lines.extend(zero_activity_note_lines(session_groups))

	return lines


def render_session_summary_section(report: Report) -> list[str]:
	"""Render compact rows and the count of omitted zero-activity sessions.

	Args:
		report: Serialised report containing session token and driver totals.

	Returns:
		Markdown lines for the compact session summary and omitted-session note.
	"""
	# The sessions sorted into those with driver rows, those with only token usage, and those with no activity.
	session_groups = classify_sessions(report["sessions"])
	# The Markdown header and compact session rows.
	lines = [
		"## Sessions (tokens, not cost)",
		"",
		"| Session | Tool calls | Total tokens |",
		"| --- | ---: | ---: |",
	]

	for session in report["sessions"]:
		if classify_session(session) == "zero_activity":
			continue

		# The token count shown in this session's compact row.
		total_tokens = session["tokens"]["total_tokens"]
		lines.append(
			f"| {session['tool']} `{session['session_id']}` | "
			f"{session['tool_call_count']} | {display_token_count(total_tokens)} |"
		)

	# The omission note for sessions without usage or tool calls.
	zero_activity_lines = zero_activity_note_lines(session_groups)
	if zero_activity_lines:
		lines.extend(["", *zero_activity_lines])

	return lines


def render_ratios_section(report: Report) -> list[str]:
	"""Render Claude cache-read and Codex reasoning-output ratios.

	Args:
		report: Serialised report containing runtime ratio data.

	Returns:
		Markdown lines for the ratio sections.
	"""
	# The runtime totals that supply both displayed ratios.
	by_tool = report["totals_by_tool"]
	# The Markdown lines for cache-read and reasoning-output ratios.
	lines = ["", "## Claude cache-read ratio (tokens, not cost)", ""]
	# The Claude cache-read share and its token counts, when available.
	claude_ratio = by_tool["Claude"].get("cache_read_ratio")
	if by_tool["Claude"]["empty"]:
		lines.append("No Claude usage records in the selected window.")
	else:
		lines.append(
			f"Cache-read input tokens / total input tokens: **{format_ratio(claude_ratio)}** "
			f"({display_token_count(claude_ratio['numerator_tokens'])} / "
			f"{display_token_count(claude_ratio['denominator_tokens'])})."
		)

	lines.extend(["", "## Codex reasoning-output ratio (tokens, not cost)", ""])
	# The Codex reasoning-output share and its token counts, when available.
	codex_ratio = by_tool["Codex"].get("reasoning_output_ratio")
	if by_tool["Codex"]["empty"]:
		lines.append("No Codex token_count records in the selected window.")
	else:
		lines.append(
			f"Reasoning output tokens / output tokens: **{format_ratio(codex_ratio)}** "
			f"({display_token_count(codex_ratio['numerator_tokens'])} / "
			f"{display_token_count(codex_ratio['denominator_tokens'])})."
		)

	return lines


def render_semantic_notes_section() -> list[str]:
	"""Render the report's token-counting semantics note.

	Returns:
		Markdown lines describing Codex token-counting semantics.
	"""
	return [
		"",
		"## Codex counting semantics (tokens, not cost)",
		"",
		"`total_token_usage` is cumulative per session. `last_token_usage` is the per-event delta, "
		"which is what this report sums.",
		"",
	]


def render_markdown(report: Report) -> str:
	"""Render the compact Markdown summary.

	Args:
		report: Serialised report to render.

	Returns:
		The complete Markdown report with a trailing newline.
	"""
	# The Markdown lines for the compact report summary.
	lines = [
		"# Token usage summary",
		"",
		"All figures below are tokens, not cost. The report contains no price or dollar estimate.",
		"",
	]
	lines.extend(render_session_summary_section(report))

	return "\n".join(lines) + "\n"


def render_detail_markdown(report: Report) -> str:
	"""Render the complete Markdown report with per-session driver tables.

	Args:
		report: Serialised report to render.

	Returns:
		The complete Markdown report with a trailing newline.
	"""
	# The Markdown lines for the full report detail.
	lines = [
		"# Token usage report detail",
		"",
		"All figures below are tokens, not cost. The report contains no price or dollar estimate.",
		"",
	]
	lines.extend(render_window_section(report))
	lines.extend(render_tool_totals_section(report))
	lines.extend(render_group_tables_section(report))
	lines.extend(render_top_sessions_section(report))
	lines.extend(render_driver_views_section(report))
	lines.extend(render_ratios_section(report))
	lines.extend(render_semantic_notes_section())

	return "\n".join(lines) + "\n"
