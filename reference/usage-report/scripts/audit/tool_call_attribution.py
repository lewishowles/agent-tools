"""Classify tool calls and build per-session driver ledgers."""

from __future__ import annotations

import math

from metrics import COMMAND_TEXT_LIMIT, RESULT_TEXT_LIMIT, classify_bash_command
from redundancy import repeated_call_indexes
from token_usage_types import (
	DriverCall,
	DriverClassification,
	DriverLedgerRow,
	DriverRule,
	RecordStats,
	Session,
)


# The estimation method, four characters per token, recorded with every payload estimate.
DRIVER_METHOD = "chars/4"


def safe_text(value: object) -> str:
	"""Return a single-line string suitable for safe length measurement.

	Args:
		value: Value to normalise when it is a string.

	Returns:
		A stripped, single-line string, or an empty string for other values.
	"""
	if not isinstance(value, str):
		return ""

	return value.replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()


def result_text(value: object) -> str:
	"""Extract result text for bounded length measurement without reporting it.

	Args:
		value: Tool result value, possibly nested in content fields.

	Returns:
		The bounded-measurement source text, or an empty string when absent.
	"""
	if isinstance(value, str):
		return value

	if isinstance(value, list):
		return " ".join(result_text(item) for item in value)

	if isinstance(value, dict):
		for key in ("content", "output", "result", "text"):
			if key in value:
				return result_text(value[key])

	return ""


def tool_result_failed(value: object) -> bool:
	"""Return whether a tool result explicitly reports a failure.

	Args:
		value: Tool result value to inspect.

	Returns:
		True when the result contains an error flag, otherwise False.
	"""
	return isinstance(value, dict) and bool(
		value.get("is_error") or value.get("isError")
	)


def command_input(tool_input: dict[str, object]) -> str:
	"""Return a command from Claude or Codex tool input.

	Args:
		tool_input: Decoded tool input object.

	Returns:
		The first non-empty command value, or an empty string when absent.
	"""
	for key in ("command", "cmd"):
		# The first non-empty command value supplied under a supported input key.
		command = safe_text(tool_input.get(key))
		if command:
			return command

	return ""


def extract_command_target(name: str, tool_input: dict[str, object]) -> str:
	"""Return a command target, including an empty target when absent.

	Args:
		name: Tool name supplied by the classification rule.
		tool_input: Decoded tool input object.

	Returns:
		The command target, or an empty string when absent.
	"""
	return command_input(tool_input)


def extract_file_target(name: str, tool_input: dict[str, object]) -> str | None:
	"""Return a non-empty file target from tool input.

	Args:
		name: Tool name supplied by the classification rule.
		tool_input: Decoded tool input object.

	Returns:
		The normalised file target, or None when absent.
	"""
	# The file path supplied for a read, write, or edit call, when present.
	target = safe_text(tool_input.get("file_path") or tool_input.get("path"))
	return target or None


def extract_skill_target(name: str, tool_input: dict[str, object]) -> str | None:
	"""Return a non-empty skill target from tool input.

	Args:
		name: Tool name supplied by the classification rule.
		tool_input: Decoded tool input object.

	Returns:
		The normalised skill target, or None when absent.
	"""
	# The skill name supplied for a skill call, when present.
	target = safe_text(tool_input.get("skill") or tool_input.get("name"))
	return target or None


# Define the fixed-name rules before adding the dynamic hook and MCP rules.
DRIVER_RULES: dict[str, DriverRule] = {
	"Bash": {
		"category": "bash",
		"target_extractor": extract_command_target,
	},
	"Edit": {
		"category": "edit",
		"target_extractor": extract_file_target,
	},
	"Read": {
		"category": "read",
		"target_extractor": extract_file_target,
	},
	"Skill": {
		"category": "skill",
		"target_extractor": extract_skill_target,
	},
	"Write": {
		"category": "write",
		"target_extractor": extract_file_target,
	},
	"exec_command": {
		"category": "bash",
		"target_extractor": extract_command_target,
	},
}


def extract_hook_target(name: str, tool_input: dict[str, object]) -> str:
	"""Return a hook name from tool input, falling back to the tool name.

	Args:
		name: Hook tool name used as the fallback.
		tool_input: Decoded tool input object.

	Returns:
		The hook target, or the tool name when absent.
	"""
	return safe_text(tool_input.get("name") or name)


def extract_name_target(name: str, tool_input: dict[str, object]) -> str:
	"""Return the tool name when it is also the classification target.

	Args:
		name: Tool name used as the target.
		tool_input: Decoded tool input object.

	Returns:
		The supplied tool name.
	"""
	return name


def driver_rule(name: str) -> DriverRule | None:
	"""Return the table or dynamic rule for a tool name.

	Args:
		name: Tool name to classify.

	Returns:
		The matching classification rule, or None for unsupported tools.
	"""
	# The fixed classification rule for this tool name, when one exists.
	rule = DRIVER_RULES.get(name)
	if rule is not None:
		return rule

	if name == "Hook" or name.lower().startswith("hook"):
		return {"category": "hook", "target_extractor": extract_hook_target}

	if name.startswith(("mcp__", "mcp_")):
		return {"category": "mcp", "target_extractor": extract_name_target}

	return None


def driver_repeat_identity(
	name: str,
	category: str,
	target: str,
) -> tuple[str, dict[str, object]]:
	"""Return the tool identity and bounded input used for repeat detection.

	Args:
		name: Tool name being classified.
		category: Classification category for the tool.
		target: Extracted target used by the classifier.

	Returns:
		The repeat-detection tool name and bounded input object.
	"""
	if category == "bash":
		return "Bash", {"command": target}

	if category in ("read", "write", "edit"):
		return name, {"file_path": target}

	return name, {}


def build_driver_classification(
	name: str,
	category: str,
	target: str,
	classification_key: str | None = None,
) -> DriverClassification:
	"""Build a classification from its category, target, and repeat identity.

	Args:
		name: Tool name being classified.
		category: Classification category for the tool.
		target: Extracted target used by the classifier.
		classification_key: Optional explicit grouping key.

	Returns:
		The completed driver classification.
	"""
	# The tool name and input used to find repeated calls.
	repeat_name, repeat_input = driver_repeat_identity(name, category, target)
	# The grouping key supplied by the caller or derived from the target.
	key = classification_key or target
	if classification_key is None and category == "bash":
		key = classify_bash_command(target) or "other"

	return {
		"category": category,
		"key": key,
		"target": target,
		"repeat_name": repeat_name,
		"repeat_input": repeat_input,
	}


def fallback_driver_classification(
	name: str,
	tool_input: dict[str, object],
) -> DriverClassification:
	"""Classify an unknown tool using its safest available target.

	Args:
		name: Unknown tool name.
		tool_input: Decoded tool input object.

	Returns:
		A fallback classification using the safest available target.
	"""
	# The safest available target for an otherwise unknown tool.
	target = safe_text(
		tool_input.get("file_path")
		or tool_input.get("path")
		or tool_input.get("subagent_type")
		or name
	)
	return build_driver_classification(name, "tool", target, name)


def driver_classification(
	name: object,
	tool_input: dict[str, object],
) -> DriverClassification | None:
	"""Classify one tool call and return its safe key and estimate target.

	Args:
		name: Tool name read from a transcript record.
		tool_input: Decoded tool input object.

	Returns:
		The safe classification, or None when the tool cannot be classified.
	"""
	if not isinstance(name, str) or not name:
		return None

	# The fixed or dynamic rule used to classify this tool name.
	rule = driver_rule(name)
	if rule is None:
		return fallback_driver_classification(name, tool_input)

	# The target extracted by the tool's rule, when present.
	target = rule["target_extractor"](name, tool_input)
	if target is None:
		return None

	return build_driver_classification(name, rule["category"], target)


def new_driver_call(name: object, tool_input: dict[str, object]) -> DriverCall:
	"""Create an internal tool-call record without retaining raw result content.

	Args:
		name: Tool name read from a transcript record.
		tool_input: Decoded tool input object.

	Returns:
		The bounded internal tool-call record.
	"""
	# The driver grouping attached to this parsed call, when available.
	classification = driver_classification(name, tool_input)
	return {
		"name": name,
		"input": tool_input,
		"classification": classification,
		"result": "",
		"failed": False,
	}


def new_session_parse_state() -> tuple[dict[str, DriverCall], RecordStats]:
	"""Create driver correlation and record-skipping state for one session.

	Returns:
		Empty response-correlation mapping and record-skipping counter.
	"""
	return {}, {"skipped_record_count": 0}


def append_driver_call(
	session: Session,
	calls_by_id: dict[str, DriverCall],
	call: DriverCall,
	call_id: object = None,
) -> None:
	"""Append one in-window driver call and register its response identifier.

	Args:
		session: Session receiving the driver call.
		calls_by_id: Response identifier mapping for correlated results.
		call: Bounded driver call to append.
		call_id: Optional response identifier for the call.

	Returns:
		None. The session and mapping are updated in place.
	"""
	session["_driver_calls"].append(call)
	session["tool_call_count"] += 1

	if isinstance(call_id, str) and call_id:
		calls_by_id[call_id] = call


def update_driver_call_result(
	calls_by_id: dict[str, DriverCall],
	call_id: object,
	result: object,
	failed: bool,
) -> None:
	"""Attach one correlated result to a previously recorded driver call.

	Args:
		calls_by_id: Response identifier mapping for recorded calls.
		call_id: Response identifier to resolve.
		result: Tool result value to measure.
		failed: Whether the result explicitly reports failure.

	Returns:
		None. The matching call is updated when the identifier is known.
	"""
	# The earlier tool call matched by this result identifier.
	call = calls_by_id.get(call_id)
	if call is not None:
		call["result"] = result_text(result)
		call["failed"] = failed


def estimated_payload_tokens(call: DriverCall) -> int:
	"""Estimate one driver payload from bounded command, target, and result text.

	Args:
		call: Bounded driver call to measure.

	Returns:
		The estimated payload size in tokens.
	"""
	# The grouping that supplies the target text for this call.
	classification = call["classification"]
	if classification is None:
		# The tool name used as the target when the call has no grouping.
		target = safe_text(call.get("name"))
	else:
		target = classification["target"]

	# The target and result text, cut to their length limits, used for the estimate.
	parts = [target[:COMMAND_TEXT_LIMIT], safe_text(call["result"])[:RESULT_TEXT_LIMIT]]
	# The number of characters across both payload parts.
	character_count = sum(len(part) for part in parts)
	return math.ceil(character_count / 4) if character_count else 0


def finalise_driver_ledger(session: Session) -> None:
	"""Build ranked driver rows and explicit unattributed counts for a session.

	Args:
		session: Session whose parsed driver calls should be aggregated.

	Returns:
		None. Driver ledger fields are updated in the session.
	"""
	# The parsed tool calls to group into this session's ledger.
	calls = session["_driver_calls"]
	# The call identities and positions used to detect repetitions.
	repetition_calls = [
		(
			call["classification"]["repeat_name"]
			if call["classification"]
			else call["name"],
			call["classification"]["repeat_input"] if call["classification"] else {},
			index,
		)
		for index, call in enumerate(calls)
	]
	# The call positions identified as repeats of earlier calls.
	repeated_indexes = repeated_call_indexes(repetition_calls)
	# The grouped ledger rows indexed by category and key.
	rows: dict[tuple[str, str], DriverLedgerRow] = {}
	# The number of calls without a driver grouping.
	unattributed_count = 0
	# The estimated payload tokens for calls without a grouping.
	unattributed_payload = 0
	# Whether an earlier call with the same category and key failed.
	failed_before: dict[tuple[str, str], bool] = {}

	for index, call in enumerate(calls):
		# The grouping attached to this parsed call, when available.
		classification = call["classification"]
		# The estimated payload tokens for this call.
		payload_estimate = estimated_payload_tokens(call)
		if classification is None:
			unattributed_count += 1
			unattributed_payload += payload_estimate
			continue

		# The report category assigned to this classified call.
		category = classification["category"]
		# The key that groups matching calls in the ledger.
		key = classification["key"]
		# The category and key used to find this call's ledger row.
		identity = (category, key)
		# The number of retries added when an earlier matching call failed.
		retry_count = 1 if failed_before.get(identity) else 0
		if call["failed"]:
			failed_before[identity] = True

		# The existing or newly created ledger row for this call's identity.
		row = rows.setdefault(
			identity,
			{
				"category": category,
				"key": key,
				"count": 0,
				"payload_estimate_tokens": 0,
				"method": DRIVER_METHOD,
				"failure_count": 0,
				"retry_count": 0,
				"repeated": False,
			},
		)
		row["count"] += 1
		row["payload_estimate_tokens"] += payload_estimate
		row["failure_count"] += int(call["failed"])
		row["retry_count"] += retry_count
		row["repeated"] = row["repeated"] or index in repeated_indexes

	# The ledger rows ranked by estimated payload, then by category and key.
	ordered_rows = sorted(
		rows.values(),
		key=lambda row: (-row["payload_estimate_tokens"], row["category"], row["key"]),
	)
	session["driver_ledger"] = ordered_rows
	session["unattributed_count"] = unattributed_count
	session["unattributed"] = {
		"count": unattributed_count,
		"payload_estimate_tokens": unattributed_payload,
		"method": DRIVER_METHOD,
	}
	session["driver_reconciles"] = session["tool_call_count"] == (
		sum(row["count"] for row in ordered_rows) + unattributed_count
	)
