"""Parse Claude and Codex transcripts into typed usage sessions."""

from __future__ import annotations

import datetime
import json
import math
import re
from pathlib import Path
from typing import Iterator

from tool_call_attribution import (
	DRIVER_METHOD,
	append_driver_call,
	finalise_driver_ledger,
	new_driver_call,
	new_session_parse_state,
	result_text,
	safe_text,
	tool_result_failed,
	update_driver_call_result,
)
from token_usage_types import (
	CODEX_FIELDS,
	TOKEN_SCHEMAS,
	UNATTRIBUTED,
	DriverCall,
	RecordStats,
	Session,
	TokenSchema,
	TokenTotals,
)

# The Claude record types that can contain usage, tool calls, or session metadata.
CLAUDE_RECORD_TYPES = (
	"assistant",
	"attachment",
	"user",
	"last-prompt",
	"mode",
	"permission-mode",
	"file-history-snapshot",
	"ai-title",
	"system",
	"queue-operation",
)

# The Codex record types that can contain usage, tool calls, or session metadata.
CODEX_RECORD_TYPES = (
	"event_msg",
	"response_item",
	"session_meta",
	"turn_context",
	"compacted",
)

# Match a quoted or bare JavaScript property and capture its quoted string value.
EMBEDDED_JS_ARGUMENT_PATTERN = (
	r"""(?:['"]{key}['"]|{key})\s*:\s*"""
	r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
)

# Match the first file path named by an apply_patch source string.
EMBEDDED_PATCH_PATH_PATTERN = re.compile(
	r"\*\*\* (?:Update|Add|Delete) File:\s*([^\s\\]+)"
)

# Match the tool name in a nested Codex exec source string.
EMBEDDED_TOOL_CALL_PATTERN = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def parse_timestamp(value: object) -> datetime.datetime | None:
	"""Parse a transcript timestamp, treating naive values as UTC.

	Args:
		value: The timestamp value read from a JSONL record.

	Returns:
		An aware UTC timestamp, or None for an unsupported value.
	"""
	if not isinstance(value, str) or not value:
		return None

	try:
		# The UTC timestamp used to decide whether this record falls inside the report window.
		timestamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError:
		return None

	if timestamp.tzinfo is None:
		timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)

	return timestamp.astimezone(datetime.timezone.utc)


def blocks(record: dict[str, object]) -> Iterator[dict[str, object]]:
	"""Yield the content blocks of a user or assistant record."""
	# The text or blocks carried by the record message.
	content = (record.get("message") or {}).get("content")

	if isinstance(content, str):
		yield {"type": "text", "text": content}
	elif isinstance(content, list):
		for block in content:
			if isinstance(block, dict):
				yield block


def records(
	path: Path,
	supported_types: tuple[str, ...],
	record_stats: RecordStats,
) -> Iterator[dict[str, object]]:
	"""Yield supported JSON objects and count skipped transcript records.

	Args:
		path: Transcript path to read as JSONL.
		supported_types: Record types accepted by the caller's parser.
		record_stats: Mutable counter updated for malformed or unsupported records.

	Returns:
		An iterator over supported JSON object records.
	"""
	with path.open(encoding="utf-8", errors="replace") as handle:
		for line in handle:
			try:
				# The JSON object read from one transcript line.
				record = json.loads(line)
			except (TypeError, ValueError):
				record_stats["skipped_record_count"] += 1
				continue

			if (
				not isinstance(record, dict)
				or record.get("type") not in supported_types
			):
				record_stats["skipped_record_count"] += 1
				continue

			yield record


def number(value: object) -> int:
	"""Return a non-negative integer token value for a parsed field.

	Args:
		value: Parsed token field value.

	Returns:
		A finite value truncated toward zero and clamped to zero, or zero for an
		unsupported or non-finite value.
	"""
	if isinstance(value, bool) or not isinstance(value, (int, float)):
		return 0

	if isinstance(value, float) and not math.isfinite(value):
		return 0

	return max(0, int(value))


def object_input(value: object) -> dict[str, object]:
	"""Decode a tool input object when a transcript stores it as JSON text.

	Args:
		value: Tool input object or JSON text.

	Returns:
		The decoded object, or an empty object when decoding is unsupported.
	"""
	if isinstance(value, dict):
		return value

	if not isinstance(value, str):
		return {}

	try:
		# The decoded argument object from a JSON string.
		decoded = json.loads(value)
	except (TypeError, ValueError):
		return {}

	return decoded if isinstance(decoded, dict) else {}


def decode_js_string(value: object) -> str:
	"""Decode one quoted JavaScript string literal without parsing the source.

	Args:
		value: Quoted JavaScript string literal.

	Returns:
		The decoded single-line string, or an empty string for invalid input.
	"""
	if not isinstance(value, str) or len(value) < 2:
		return ""

	if value.startswith('"') and value.endswith('"'):
		try:
			return safe_text(json.loads(value))
		except (TypeError, ValueError):
			return ""

	if value.startswith("'") and value.endswith("'"):
		try:
			return safe_text(bytes(value[1:-1], "utf-8").decode("unicode_escape"))
		except (UnicodeDecodeError, ValueError):
			return ""

	return ""


def embedded_js_argument(source: object, key: str) -> str:
	"""Extract one quoted argument from a nested JavaScript tool call.

	Args:
		source: JavaScript source containing a nested tool call.
		key: Property name whose quoted value should be extracted.

	Returns:
		The decoded property value, or an empty string when it is absent.
	"""
	if not isinstance(source, str):
		return ""

	# The property pattern for the requested nested JavaScript argument.
	pattern = EMBEDDED_JS_ARGUMENT_PATTERN.format(key=re.escape(key))
	# The quoted value found for the requested property, when present.
	match = re.search(pattern, source)
	return decode_js_string(match.group(1)) if match else ""


def embedded_patch_path(source: object) -> str:
	"""Extract the first safe target path from an apply_patch source string.

	Args:
		source: apply_patch source containing a file header.

	Returns:
		The first safe target path, or an empty string when no header is present.
	"""
	if not isinstance(source, str):
		return ""

	# The first file header found in the nested patch text, when present.
	match = EMBEDDED_PATCH_PATH_PATTERN.search(source)
	return safe_text(match.group(1)) if match else ""


def embedded_tool_call(value: object) -> tuple[str, dict[str, object]] | None:
	"""Extract the real tool call nested inside a Codex ``exec`` source string.

	Args:
		value: Codex exec source containing a nested tool call.

	Returns:
		The extracted tool name and input, or None when no call is present.
	"""
	if not isinstance(value, str):
		return None

	# The first nested tool call found in the Codex exec source.
	match = EMBEDDED_TOOL_CALL_PATTERN.search(value)
	if not match:
		return None

	# The actual tool name called inside the Codex exec source.
	name = match.group(1)
	# The supported argument values recovered from the nested tool call.
	tool_input = {
		key: argument
		for key in (
			"agent",
			"command",
			"cmd",
			"file_path",
			"name",
			"path",
			"skill",
			"subagent_type",
		)
		# The non-empty quoted argument retained for each supported input key.
		if (argument := embedded_js_argument(value, key))
	}

	if name == "apply_patch":
		# The file path named by the nested patch, when present.
		file_path = embedded_patch_path(value)
		if file_path:
			tool_input["file_path"] = file_path

	if name == "exec_command" or name == "write_stdin":
		return "Bash", tool_input

	if name == "apply_patch":
		return "Edit", tool_input

	return name, tool_input


def token_schema(tool: str) -> TokenSchema:
	"""Return the token field schema for one runtime.

	Args:
		tool: Runtime name whose token fields are needed.

	Returns:
		The matching token schema, defaulting to Codex fields.
	"""
	return TOKEN_SCHEMAS.get(tool, TOKEN_SCHEMAS["Codex"])


def empty_totals(tool: str) -> TokenTotals:
	"""Create the token fields used by one tool's aggregate.

	Args:
		tool: Runtime name whose token fields are needed.

	Returns:
		A zero-valued token aggregate for the runtime.
	"""
	# The token fields required by this runtime.
	schema = token_schema(tool)
	# The zero-valued source token fields for a new aggregate.
	totals = {field: 0 for field in schema["fields"]}
	totals["total_tokens"] = 0

	for field in schema["total_input_fields"]:
		totals[field] = 0

	return totals


def usage_totals(usage: dict[str, object], tool: str) -> TokenTotals:
	"""Normalise one Claude or Codex usage object into token fields.

	Args:
		usage: Raw usage object from a transcript record.
		tool: Runtime name whose token fields are needed.

	Returns:
		The normalised non-negative token totals.
	"""
	# The token fields required by this runtime.
	schema = token_schema(tool)
	# The non-negative usage values for the runtime's source fields.
	values = {field: number(usage.get(field)) for field in schema["fields"]}
	# The input fields added together for the combined input total; Codex has none.
	total_input_fields = schema["total_input_fields"]

	if total_input_fields:
		values["total_input_tokens"] = sum(
			values[field] for field in total_input_fields
		)
		values["total_tokens"] = values["total_input_tokens"] + values["output_tokens"]
	elif not values["total_tokens"]:
		values["total_tokens"] = values["input_tokens"] + values["output_tokens"]

	return values


def add_totals(target: TokenTotals, source: TokenTotals, tool: str) -> None:
	"""Add one normalised usage object to an aggregate.

	Args:
		target: Aggregate updated in place.
		source: Normalised usage totals to add.
		tool: Runtime name whose total-input fields are needed.

	Returns:
		None. The target aggregate is updated in place.
	"""
	# The token fields needed to update the target aggregate.
	schema = token_schema(tool)
	for field, value in source.items():
		if field != "total_input_tokens" or schema["total_input_fields"]:
			target[field] = target.get(field, 0) + value

	if schema["total_input_fields"]:
		target["total_input_tokens"] = sum(
			target[field] for field in schema["total_input_fields"]
		)
		target["total_tokens"] = target["total_input_tokens"] + target["output_tokens"]


def add_session_usage(
	session: Session,
	usage: dict[str, object],
	model: str,
	day: str,
	tool: str,
) -> None:
	"""Add one in-window event to session, model, and day totals.

	Args:
		session: Session receiving the usage event.
		usage: Raw usage object from the transcript.
		model: Model name used by the event.
		day: UTC calendar day used by the event.
		tool: Runtime name whose totals are being updated.

	Returns:
		None. Session, model, and day aggregates are updated in place.
	"""
	# The usage event in the common token format used by report totals.
	normalised = usage_totals(usage, tool)
	if not normalised["total_tokens"]:
		return

	add_totals(session["tokens"], normalised, tool)
	add_totals(
		session["models"].setdefault(model, empty_totals(tool)), normalised, tool
	)
	add_totals(session["days"].setdefault(day, empty_totals(tool)), normalised, tool)


def new_session(
	tool: str, session_id: str, path: Path, project_directory: str
) -> Session:
	"""Create the internal representation for one transcript session.

	Args:
		tool: Runtime that produced the transcript.
		session_id: Stable identifier for the transcript session.
		path: Transcript path used in the report.
		project_directory: Initial project directory from the transcript location.

	Returns:
		A zero-valued internal session model.
	"""
	return {
		"tool": tool,
		"session_id": session_id,
		"transcript_path": str(path),
		"project_directory": project_directory or "unknown",
		"tokens": empty_totals(tool),
		"models": {},
		"days": {},
		"hcom": dict(UNATTRIBUTED),
		"_driver_calls": [],
		"tool_call_count": 0,
		"skipped_record_count": 0,
		"driver_ledger": [],
		"unattributed_count": 0,
		"unattributed": {
			"count": 0,
			"payload_estimate_tokens": 0,
			"method": DRIVER_METHOD,
		},
		"driver_reconciles": True,
	}


def finalise_session(session: Session, record_stats: RecordStats) -> Session | None:
	"""Attach parser bookkeeping and omit sessions with no usage or skipped records.

	Args:
		session: Parsed session to finalise.
		record_stats: Record-skipping counter from transcript reading.

	Returns:
		The session when it has usage or skipped records, otherwise None.
	"""
	session["skipped_record_count"] = record_stats["skipped_record_count"]
	finalise_driver_ledger(session)

	if session["tokens"]["total_tokens"] or session["skipped_record_count"]:
		return session

	return None


def in_window(
	timestamp: datetime.datetime | None,
	start: datetime.datetime,
	end: datetime.datetime,
) -> bool:
	"""Return whether a timestamp belongs to the selected half-open window.

	Args:
		timestamp: Timestamp to test.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		True when the timestamp is inside the window, otherwise False.
	"""
	return timestamp is not None and start <= timestamp < end


def update_claude_metadata(session: Session, record: dict[str, object]) -> None:
	"""Update the project directory from one Claude record.

	Args:
		session: Session receiving metadata.
		record: Claude transcript record to inspect.

	Returns:
		None. The session project directory is updated in place when present.
	"""
	# The working directory recorded for this Claude session, when present.
	cwd = record.get("cwd")
	if isinstance(cwd, str) and cwd:
		session["project_directory"] = cwd


def append_claude_hook_call(
	session: Session,
	calls_by_id: dict[str, DriverCall],
	record: dict[str, object],
	timestamp: datetime.datetime | None,
	start: datetime.datetime,
	end: datetime.datetime,
) -> None:
	"""Record one in-window Claude hook attachment as a driver call.

	Args:
		session: Session receiving the hook call.
		calls_by_id: Response identifier mapping for correlated results.
		record: Claude transcript record to inspect.
		timestamp: Parsed UTC timestamp for the record.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		None. An in-window hook attachment is appended when valid.
	"""
	# The attachment that may describe a successful Claude hook call.
	attachment = record.get("attachment")
	if not (
		isinstance(attachment, dict)
		and attachment.get("type") == "hook_success"
		and in_window(timestamp, start, end)
	):
		return

	# The single-line hook name used to group this attachment with other calls.
	hook_name = safe_text(attachment.get("hookName"))
	if not hook_name:
		return

	# The hook call retained for driver attribution.
	call = new_driver_call("Hook", {"name": hook_name})
	call["result"] = result_text(attachment.get("stdout"))
	call["failed"] = attachment.get("exitCode") not in (None, 0, "0")
	append_driver_call(session, calls_by_id, call)


def process_claude_tool_blocks(
	session: Session,
	calls_by_id: dict[str, DriverCall],
	record: dict[str, object],
	timestamp: datetime.datetime | None,
	start: datetime.datetime,
	end: datetime.datetime,
) -> None:
	"""Correlate Claude tool calls and their results for one record.

	Args:
		session: Session receiving in-window tool calls.
		calls_by_id: Response identifier mapping for correlated results.
		record: Claude transcript record to inspect.
		timestamp: Parsed UTC timestamp for the record.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		None. Tool calls and results are recorded in the session state.
	"""
	for block in blocks(record):
		if block.get("type") == "tool_use" and in_window(timestamp, start, end):
			# The Claude tool call retained for driver attribution.
			call = new_driver_call(
				block.get("name"), object_input(block.get("input") or {})
			)
			append_driver_call(session, calls_by_id, call, block.get("id"))
		elif block.get("type") == "tool_result":
			update_driver_call_result(
				calls_by_id,
				block.get("tool_use_id"),
				block.get("content"),
				tool_result_failed(block),
			)


def claude_usage_event(
	record: dict[str, object],
	timestamp: datetime.datetime | None,
	start: datetime.datetime,
	end: datetime.datetime,
) -> tuple[dict[str, object], str, str] | None:
	"""Return one in-window Claude usage event with its model and day.

	Args:
		record: Claude transcript record to inspect.
		timestamp: Parsed UTC timestamp for the record.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		The usage object, model, and UTC day, or None when out of scope.
	"""
	if record.get("type") != "assistant":
		return None

	# The assistant message that may contain token usage.
	message = record.get("message") or {}
	# Sidechain (subagent) usage lives only inside its own record here, never
	# duplicated in the parent's usage, so it is counted rather than filtered.
	usage = message.get("usage")
	if not isinstance(usage, dict) or not in_window(timestamp, start, end):
		return None

	# The model name attached to this assistant usage event.
	model = message.get("model")
	model = model if isinstance(model, str) and model else "unknown"
	return usage, model, timestamp.date().isoformat()


def aggregate_claude_usage(
	session: Session,
	event: tuple[dict[str, object], str, str] | None,
) -> None:
	"""Add one parsed Claude usage event to the session totals.

	Args:
		session: Session receiving the usage event.
		event: Parsed usage object, model, and UTC day, or None.

	Returns:
		None. Session aggregates are updated in place when an event is present.
	"""
	if event is None:
		return

	# The usage values, model name, and UTC day carried by this event.
	usage, model, day = event
	add_session_usage(session, usage, model, day, "Claude")


def parse_claude_session(
	path: Path,
	start: datetime.datetime,
	end: datetime.datetime,
) -> Session | None:
	"""Parse in-window Claude usage records from one transcript.

	Args:
		path: Claude transcript path to parse.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		The parsed session, or None when it has no usage or skipped records.
	"""
	# The session identifier taken from the Claude transcript filename.
	session_id = path.stem
	# The directory name used until the transcript supplies a working directory.
	project_directory = path.parent.name
	# The session aggregate that receives this transcript's usage and tool calls.
	session = new_session("Claude", session_id, path, project_directory)
	# The call lookup for matching results and the skipped-record counter.
	calls_by_id, record_stats = new_session_parse_state()

	for record in records(path, CLAUDE_RECORD_TYPES, record_stats):
		update_claude_metadata(session, record)
		# The record timestamp used to filter usage and tool calls by date.
		timestamp = parse_timestamp(record.get("timestamp"))
		append_claude_hook_call(session, calls_by_id, record, timestamp, start, end)
		process_claude_tool_blocks(session, calls_by_id, record, timestamp, start, end)
		aggregate_claude_usage(
			session, claude_usage_event(record, timestamp, start, end)
		)

	return finalise_session(session, record_stats)


def codex_delta(
	last_usage: object,
	total_usage: object,
	previous_total: object,
) -> TokenTotals | None:
	"""Return one Codex event delta, preferring the recorded per-event value.

	Args:
		last_usage: Per-event token usage, when recorded.
		total_usage: Cumulative token usage, when recorded.
		previous_total: Previous cumulative token usage for this session.

	Returns:
		The event delta, or None when neither usage representation is available.
	"""
	if isinstance(last_usage, dict):
		return last_usage

	if isinstance(total_usage, dict) and isinstance(previous_total, dict):
		return {
			field: number(total_usage.get(field)) - number(previous_total.get(field))
			for field in CODEX_FIELDS
		}

	if isinstance(total_usage, dict):
		return total_usage

	return None


def update_codex_metadata(
	session: Session,
	record: dict[str, object],
	model: str,
) -> str:
	"""Update Codex session metadata and return the current model.

	Args:
		session: Session receiving metadata.
		record: Codex transcript record to inspect.
		model: Current model name before this record.

	Returns:
		The current model name after applying the record.
	"""
	# The metadata object carried by this Codex record, when present.
	payload = record.get("payload")
	if not isinstance(payload, dict):
		return model

	# The Codex record kind that determines which metadata applies.
	record_type = record.get("type")
	if record_type == "session_meta":
		# The session identifier recorded in Codex metadata, when present.
		metadata_session_id = payload.get("session_id") or payload.get("id")
		if isinstance(metadata_session_id, str) and metadata_session_id:
			session["session_id"] = metadata_session_id

	elif record_type == "turn_context":
		# The model name recorded for the current Codex turn, when present.
		context_model = payload.get("model")
		if isinstance(context_model, str) and context_model:
			model = context_model

	if record_type in ("session_meta", "turn_context"):
		# The working directory recorded for this Codex turn or session.
		cwd = payload.get("cwd")
		if isinstance(cwd, str) and cwd:
			session["project_directory"] = cwd

	return model


def decode_codex_tool_call(
	payload: dict[str, object],
) -> tuple[object, dict[str, object]]:
	"""Decode a Codex response item, including embedded JavaScript calls.

	Args:
		payload: Codex response-item payload to decode.

	Returns:
		The decoded tool name and input object.
	"""
	# The tool name recorded by the Codex response item.
	name = payload.get("name") or payload.get("tool_name")
	# The raw arguments supplied to the Codex tool call.
	arguments = payload.get("arguments", payload.get("input", {}))
	# The decoded argument object retained for tool attribution.
	tool_input = object_input(arguments)
	if payload.get("type") == "custom_tool_call":
		# The actual tool call inside a custom Codex exec item, when present.
		embedded = embedded_tool_call(arguments)
		if embedded is not None:
			return embedded

	return name, tool_input


def process_codex_response_item(
	session: Session,
	calls_by_id: dict[str, DriverCall],
	record: dict[str, object],
	timestamp: datetime.datetime | None,
	start: datetime.datetime,
	end: datetime.datetime,
) -> None:
	"""Correlate Codex response-item tool calls and their results.

	Args:
		session: Session receiving in-window tool calls.
		calls_by_id: Response identifier mapping for correlated results.
		record: Codex transcript record to inspect.
		timestamp: Parsed UTC timestamp for the record.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		None. Tool calls and results are recorded in the session state.
	"""
	if record.get("type") != "response_item":
		return

	# The response item that may contain a Codex tool call or result.
	payload = record.get("payload")
	if not isinstance(payload, dict):
		return

	# The item kind that determines whether to record a call or result.
	response_type = payload.get("type")
	if response_type in ("function_call", "custom_tool_call"):
		if not in_window(timestamp, start, end):
			return

		# The decoded tool name and arguments for this Codex call.
		name, tool_input = decode_codex_tool_call(payload)
		# The Codex tool call retained for driver attribution.
		call = new_driver_call(name, tool_input)
		# The identifier used to match this call with its later result.
		call_id = payload.get("call_id") or payload.get("id")
		append_driver_call(session, calls_by_id, call, call_id)
	elif response_type in ("function_call_output", "custom_tool_call_output"):
		call_id = payload.get("call_id") or payload.get("id")
		update_driver_call_result(
			calls_by_id,
			call_id,
			payload.get("output"),
			tool_result_failed(payload),
		)


def codex_usage_event(
	record: dict[str, object],
	previous_total: dict[str, object] | None,
) -> tuple[TokenTotals | None, dict[str, object] | None]:
	"""Return a Codex token delta and the next cumulative total.

	Args:
		record: Codex transcript record to inspect.
		previous_total: Previous cumulative token usage for this session.

	Returns:
		The event delta and the cumulative total for the next record.
	"""
	if record.get("type") != "event_msg":
		return None, previous_total

	# The event body that may contain Codex token counts.
	payload = record.get("payload")
	if not isinstance(payload, dict) or payload.get("type") != "token_count":
		return None, previous_total

	# The token-count details, when present separately from the event body.
	info = payload.get("info")
	if not isinstance(info, dict):
		info = payload

	# The cumulative token usage recorded for the session so far.
	total_usage = info.get("total_token_usage")
	# The token usage recorded for this event alone, when available.
	last_usage = info.get("last_token_usage")
	# The usage increment added to the session totals, when available.
	delta = codex_delta(last_usage, total_usage, previous_total)
	if isinstance(total_usage, dict):
		previous_total = total_usage

	return delta, previous_total


def aggregate_codex_usage(
	session: Session,
	delta: TokenTotals | None,
	model: str,
	timestamp: datetime.datetime | None,
	start: datetime.datetime,
	end: datetime.datetime,
) -> None:
	"""Add one in-window Codex token delta to the session totals.

	Args:
		session: Session receiving the usage event.
		delta: Parsed token delta, or None when unavailable.
		model: Model name used by the event.
		timestamp: Parsed UTC timestamp for the event.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		None. Session aggregates are updated in place when the event is in scope.
	"""
	if not in_window(timestamp, start, end) or not isinstance(delta, dict):
		return

	add_session_usage(session, delta, model, timestamp.date().isoformat(), "Codex")


def parse_codex_session(
	path: Path,
	start: datetime.datetime,
	end: datetime.datetime,
) -> Session | None:
	"""Parse in-window Codex token-count events from one rollout transcript.

	Args:
		path: Codex rollout transcript path to parse.
		start: Inclusive UTC window start.
		end: Exclusive UTC window end.

	Returns:
		The parsed session, or None when it has no usage or skipped records.
	"""
	# The session identifier taken from the Codex transcript filename.
	session_id = path.stem
	if session_id.startswith("rollout-"):
		session_id = session_id.removeprefix("rollout-")

	# The session aggregate that receives this transcript's usage and tool calls.
	session = new_session("Codex", session_id, path, "unknown")
	# The model used for usage events until turn metadata supplies its name.
	model = "unknown"
	# The previous cumulative total used to calculate later event increments.
	previous_total = None
	# The call lookup for matching results and the skipped-record counter.
	calls_by_id, record_stats = new_session_parse_state()

	for record in records(path, CODEX_RECORD_TYPES, record_stats):
		model = update_codex_metadata(session, record, model)
		# The record timestamp used to filter usage and tool calls by date.
		timestamp = parse_timestamp(record.get("timestamp"))
		process_codex_response_item(session, calls_by_id, record, timestamp, start, end)
		# The event usage increment and next cumulative total for this transcript.
		delta, previous_total = codex_usage_event(record, previous_total)
		aggregate_codex_usage(session, delta, model, timestamp, start, end)

	return finalise_session(session, record_stats)
