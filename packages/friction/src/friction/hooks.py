"""Record Claude hook failures without allowing them to block Claude."""

import json
import os

from .database import Database
from .schema import utc_timestamp

MAX_FIELD_LENGTH = 300
DISCRIMINATOR_FIELDS = (
	"command",
	"file_path",
	"path",
	"pattern",
	"query",
	"url",
	"notebook_path",
	"description",
	"prompt",
	"old_string",
)


def _clean(value: str) -> str:
	"""Remove log delimiters and limit a raw hook value."""
	return (
		value.replace("\t", " ")
		.replace("\r", " ")
		.replace("\n", " ")[:MAX_FIELD_LENGTH]
	)


def _discriminator(value: object) -> str:
	"""Return the first recognised non-empty string from a tool input."""
	if isinstance(value, str):
		return value
	if not isinstance(value, dict):
		return ""
	for field in DISCRIMINATOR_FIELDS:
		candidate = value.get(field)
		if isinstance(candidate, str) and candidate:
			return candidate
	for candidate in value.values():
		if isinstance(candidate, str) and candidate:
			return candidate
	return ""


def record_claude_tool_failure(database: Database, raw_input: str) -> None:
	"""Record one valid Claude failure and silently ignore malformed input."""
	try:
		payload = json.loads(raw_input)
		if not isinstance(payload, dict) or not isinstance(
			payload.get("tool_name"), str
		):
			return
		tool_name = _clean(payload["tool_name"])
		if not tool_name:
			return
		discriminator = (
			_clean(_discriminator(payload.get("tool_input"))) or "unknown input"
		)
		error = payload.get("error")
		error_text = _clean(error) if isinstance(error, str) else ""
		error_text = error_text or "unknown error"
		with database.transaction() as connection:
			connection.execute(
				"INSERT INTO events (timestamp_utc, category, cwd, detail, source, tool_name, discriminator, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
				(
					utc_timestamp(),
					"tool-error",
					os.getcwd(),
					f"{tool_name}: {discriminator} — {error_text}",
					"claude-hook",
					tool_name,
					discriminator,
					error_text,
				),
			)
	except Exception:
		return
