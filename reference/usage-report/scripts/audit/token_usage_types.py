"""Define the typed boundaries for the usage report and session model."""

from __future__ import annotations

import datetime
from typing import Callable, Optional, TypedDict


class RecordStats(TypedDict):
	"""Track skipped records while reading one transcript."""

	# The number of transcript records skipped because they could not be used.
	skipped_record_count: int


class TokenTotals(TypedDict, total=False):
	"""Store the normalised token fields for one runtime."""

	# The input tokens reported by the runtime.
	input_tokens: int
	# The input tokens used to create Claude cache entries.
	cache_creation_input_tokens: int
	# The input tokens Claude read from its cache.
	cache_read_input_tokens: int
	# The input tokens Codex read from its cache.
	cached_input_tokens: int
	# The output tokens Codex spent on reasoning.
	reasoning_output_tokens: int
	# The tokens produced in the response.
	output_tokens: int
	# The input and output tokens added together, or the total Codex reports.
	total_tokens: int
	# Claude's input, cache-creation and cache-read tokens added together.
	total_input_tokens: int


class TokenSchema(TypedDict):
	"""Define source and derived token fields for one runtime."""

	# The source usage fields read from records for this runtime.
	fields: tuple[str, ...]
	# The fields added together to give this runtime's combined input total.
	total_input_fields: tuple[str, ...]


# The usage fields and input-total rules for each supported runtime.
TOKEN_SCHEMAS: dict[str, TokenSchema] = {
	"Claude": {
		"fields": (
			"input_tokens",
			"cache_creation_input_tokens",
			"cache_read_input_tokens",
			"output_tokens",
		),
		"total_input_fields": (
			"input_tokens",
			"cache_creation_input_tokens",
			"cache_read_input_tokens",
		),
	},
	"Codex": {
		"fields": (
			"input_tokens",
			"cached_input_tokens",
			"reasoning_output_tokens",
			"output_tokens",
			"total_tokens",
		),
		"total_input_fields": (),
	},
}

# Keep the Codex field order available to the existing audit helper that consumes it.
CODEX_FIELDS = TOKEN_SCHEMAS["Codex"]["fields"]
# The runtime names used for report grouping and display order.
TOOLS = ("Claude", "Codex")


class DriverClassification(TypedDict):
	"""Describe the safe grouping and repeat identity for one tool call."""

	# The report grouping assigned to this tool call.
	category: str
	# The single-line identifier used to group matching tool calls.
	key: str
	# The target text used to describe this tool call.
	target: str
	# The tool name used to compare repeated calls.
	repeat_name: str
	# The input fields kept for comparing repeated calls.
	repeat_input: dict[str, object]


# Define the target-extraction callable used by each classification rule.
# Assigned at module load (not deferred by `from __future__ import annotations`,
# which only covers annotations), so it needs `Optional[str]`: Python 3.9 doesn't
# support `str | None` as a runtime expression.
DriverTargetExtractor = Callable[[str, dict[str, object]], Optional[str]]


class DriverRule(TypedDict):
	"""Define how one known tool name supplies its classification target."""

	# The report grouping given to calls this rule matches.
	category: str
	# The function that finds the target text for this tool name.
	target_extractor: DriverTargetExtractor


class DriverCall(TypedDict):
	"""Store bounded tool-call data while a session is being parsed."""

	# The tool name as it appeared in the transcript.
	name: object
	# The tool arguments, decoded into an object, used for classification.
	input: dict[str, object]
	# The grouping assigned to this call when its tool is recognised.
	classification: DriverClassification | None
	# The tool result text used for the payload estimate.
	result: str
	# Whether the tool result indicates a failed call.
	failed: bool


class DriverLedgerRow(TypedDict):
	"""Store one ranked driver-ledger aggregate."""

	# The report grouping shared by the calls in this row.
	category: str
	# The single-line identifier used to group matching tool calls.
	key: str
	# The number of tool calls represented by this row.
	count: int
	# The estimated tokens in the calls' target and result text, each cut to its length limit.
	payload_estimate_tokens: int
	# The method used to estimate the payload tokens.
	method: str
	# The number of failed tool calls in this row.
	failure_count: int
	# The number of calls that repeated an earlier failed call.
	retry_count: int
	# Whether this row contains a repeated call.
	repeated: bool


class Unattributed(TypedDict):
	"""Store tool calls that cannot be assigned to a driver row."""

	# The number of tool calls with no driver grouping.
	count: int
	# The estimated tokens in the calls' target and result text, each cut to its length limit.
	payload_estimate_tokens: int
	# The method used to estimate the payload tokens.
	method: str


class HcomLabel(TypedDict):
	"""Store the safe hcom label joined to a transcript session."""

	# The hcom agent name attached to this transcript.
	agent_name: str
	# The hcom team role attached to this transcript.
	role: str
	# The runtime the hcom agent ran on.
	tool: str


class Session(TypedDict):
	"""Store the internal report model for one parsed transcript."""

	# The runtime that produced this transcript.
	tool: str
	# The identifier that links this row to its transcript.
	session_id: str
	# The path to the transcript behind this session.
	transcript_path: str
	# The project directory recorded for this session.
	project_directory: str
	# The normalised token totals for this session.
	tokens: TokenTotals
	# The token totals for each model used in this session.
	models: dict[str, TokenTotals]
	# The per-day token totals represented in this session.
	days: dict[str, TokenTotals]
	# The hcom agent label joined to this session.
	hcom: HcomLabel
	# The parsed tool calls retained until driver totals are calculated.
	_driver_calls: list[DriverCall]
	# The number of tool calls found in this session's transcript.
	tool_call_count: int
	# The number of transcript records skipped because they could not be used.
	skipped_record_count: int
	# The grouped tool-call rows for this session.
	driver_ledger: list[DriverLedgerRow]
	# The number of tool calls without a driver grouping.
	unattributed_count: int
	# The count and payload estimate for calls without a driver grouping.
	unattributed: Unattributed
	# Whether grouped and ungrouped calls add up to all tool calls.
	driver_reconciles: bool


class RatioData(TypedDict):
	"""Store ratio inputs and the calculated ratio for report consumers."""

	# The token count used as the top part of this ratio.
	numerator_tokens: int
	# The token count used as the base of this ratio.
	denominator_tokens: int
	# The calculated share, or None when the base is zero.
	ratio: float | None


class AggregateRow(TypedDict, total=False):
	"""Store one grouped report aggregate, including empty-tool metadata."""

	# The number of transcript sessions included in this total.
	session_count: int
	# The normalised token totals added up for this row.
	tokens: TokenTotals
	# Whether this runtime has no sessions in the selected window.
	empty: bool
	# The share of Claude input tokens read from its cache.
	cache_read_ratio: RatioData
	# The share of Codex output tokens used for reasoning.
	reasoning_output_ratio: RatioData
	# The JSON note stored when this runtime has no usage in the window.
	message: str


# The per-key, per-runtime aggregate rows used by report sections.
Group = dict[str, dict[str, AggregateRow]]
# The UTC bounds and display dates for one selected window.
Window = tuple[datetime.datetime, datetime.datetime, str, str]
# The token totals grouped by runtime, model, day, project and role.
Sections = tuple[dict[str, AggregateRow], dict[str, Group], Group, Group, Group]


class WindowReport(TypedDict):
	"""Store the selected window in the serialised report."""

	# The inclusive date shown at the start of the report window.
	since: str
	# The inclusive date shown at the end of the report window.
	until: str
	# The inclusive UTC instant used to filter transcript records.
	start_utc: str
	# The UTC instant after the last included record.
	end_utc_exclusive: str


class PartialData(TypedDict):
	"""Store skipped-record totals in the serialised report."""

	# Whether any transcript records had to be skipped.
	partial: bool
	# The number of transcript records skipped because they could not be used.
	skipped_record_count: int
	# The skipped-record totals broken down by runtime.
	skipped_record_counts: dict[str, int]


class SessionReport(TypedDict):
	"""Store one serialised session row in the report."""

	# The session position after sorting by token total.
	rank: int
	# The runtime that produced this transcript.
	tool: str
	# The identifier that links this row to its transcript.
	session_id: str
	# The path to the transcript behind this session.
	transcript_path: str
	# The project directory recorded for this session.
	project_directory: str
	# The hcom agent label joined to this session.
	hcom: HcomLabel
	# The names of the models used in this session.
	models: list[str]
	# The normalised token totals for this session.
	tokens: TokenTotals
	# The number of tool calls found in this session's transcript.
	tool_call_count: int
	# The number of transcript records skipped because they could not be used.
	skipped_record_count: int
	# The number of tool calls without a driver grouping.
	unattributed_count: int
	# The count and payload estimate for calls without a driver grouping.
	unattributed: Unattributed
	# Whether grouped and ungrouped calls add up to all tool calls.
	driver_reconciles: bool
	# The grouped tool-call rows for this session.
	driver_ledger: list[DriverLedgerRow]


class DriverReconciliation(TypedDict):
	"""Store aggregate driver rows and their reconciliation counts."""

	# The tool-call rows combined across all included sessions.
	driver_ledger: list[DriverLedgerRow]
	# The number of tool calls across all included sessions.
	tool_call_count: int
	# The number of tool calls assigned to driver rows.
	attributed_count: int
	# The number of tool calls without a driver grouping.
	unattributed_count: int
	# The count and payload estimate for calls without a driver grouping.
	unattributed: Unattributed
	# Whether driver rows and ungrouped calls account for all calls.
	reconciles: bool


class Report(TypedDict):
	"""Store the complete serialised usage report."""

	# The unit used for token figures throughout the report.
	units: str
	# The dates and UTC bounds used for this report.
	window: WindowReport
	# Whether the selected window contains no sessions.
	empty_window: bool
	# The skipped-record summary shown with incomplete results.
	partial_data: PartialData
	# The number of transcript sessions included in the report.
	session_count: int
	# The token totals grouped by source runtime.
	totals_by_tool: dict[str, AggregateRow]
	# The token totals grouped by model within each runtime.
	totals_by_model: dict[str, Group]
	# The token totals grouped by calendar day.
	totals_by_day: Group
	# The token totals grouped by project directory.
	totals_by_project: Group
	# The token totals grouped by hcom role.
	totals_by_role: Group
	# The tool-call rows combined across all included sessions.
	driver_ledger: list[DriverLedgerRow]
	# The counts that check tool-call attribution across the report.
	driver_reconciliation: DriverReconciliation
	# The session rows included in the selected window.
	sessions: list[SessionReport]
	# The highest-token session rows shown in the summary.
	top_sessions: list[SessionReport]


# The label shown when a transcript has no hcom agent match.
UNATTRIBUTED: HcomLabel = {
	"agent_name": "unattributed",
	"role": "unattributed",
	"tool": "unknown",
}
