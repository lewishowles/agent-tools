"""Write friction events and resolution records."""

import json
import os
import re
from pathlib import Path

from .database import Database
from .schema import utc_timestamp

# Categories accepted from manual friction reports.
CATEGORIES = frozenset(
	{
		"rule-ignored",
		"wrong-approach",
		"token-waste",
		"tool-misuse",
		"missing-guidance",
		"check-fail",
		"tool-error",
	}
)


def _validate_category(category: str) -> None:
	"""Reject a category that cannot be stored by the friction schema."""
	if category not in CATEGORIES:
		valid_categories = ", ".join(sorted(CATEGORIES))
		raise ValueError(
			f"unknown category {category!r}; choose from: {valid_categories}"
		)


# Matches a pattern key: lowercase words of letters and digits joined by hyphens.
KEY_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# Resolution records match either an exact detail or a pattern key.
RESOLUTION_TARGETS = frozenset({"detail", "key"})


def _validate_key(key: str) -> None:
	"""Reject a pattern key that is not lowercase kebab-case."""
	if KEY_PATTERN.fullmatch(key) is None:
		raise ValueError("key must be lowercase kebab-case (letters and digits)")


def add_event(
	database: Database, category: str, detail: str, key: str | None = None
) -> dict[str, object]:
	"""Store one manual event and return its complete persisted row.

	The key names the repeated mistake and is optional. A key that is not
	lowercase kebab-case raises ValueError, and nothing is stored.
	"""
	_validate_category(category)

	if not detail.strip():
		raise ValueError("detail must not be empty")
	if key is not None:
		_validate_key(key)

	timestamp = utc_timestamp()
	cwd = os.getcwd()
	with database.transaction() as connection:
		cursor = connection.execute(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source, key,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(timestamp, category, cwd, detail, "manual", key),
		)
		row = connection.execute(
			"""
			SELECT id, timestamp_utc, category, cwd, detail, source, key,
				tool_name, discriminator, error
			FROM events WHERE id = ?
			""",
			(cursor.lastrowid,),
		).fetchone()

	return dict(row)


def apply_keys(database: Database, file_path: str) -> dict[str, int]:
	"""Set or replace event keys from a JSON file of entry IDs to keys.

	The whole file is checked before anything is written. A malformed file,
	an unknown entry ID or a key that is not lowercase kebab-case raises
	ValueError listing every problem found, and no event changes. Details are
	never edited. Returns the number of events whose key actually changed, so
	applying the same file twice reports zero the second time.
	"""
	try:
		mapping = json.loads(Path(file_path).read_text(encoding="utf-8"))
	except json.JSONDecodeError as error:
		raise ValueError(f"invalid JSON mapping: {error.msg}") from error

	if not isinstance(mapping, dict):
		raise ValueError("mapping must be a JSON object of entry IDs to keys")

	with database.transaction() as connection:
		key_by_entry_id = {
			str(row["id"]): row["key"]
			for row in connection.execute("SELECT id, key FROM events")
		}
		problems = []
		changes = []

		for entry_id, key in mapping.items():
			if entry_id not in key_by_entry_id:
				problems.append(f"entry ID {entry_id!r} does not exist")

			if not isinstance(key, str):
				problems.append(f"entry ID {entry_id!r}: key must be a string")
				continue

			try:
				_validate_key(key)
			except ValueError as error:
				problems.append(f"entry ID {entry_id!r}: {error}")
				continue

			if entry_id in key_by_entry_id and key_by_entry_id[entry_id] != key:
				changes.append((key, int(entry_id)))

		if problems:
			raise ValueError("invalid key mapping:\n" + "\n".join(problems))

		connection.executemany("UPDATE events SET key = ? WHERE id = ?", changes)

	return {"changed": len(changes)}


def add_resolution(
	database: Database,
	category: str,
	pattern: str,
	reference: str,
	target: str = "detail",
) -> dict[str, object]:
	"""Store one resolution for a category and pattern without creating an event.

	The target says whether the pattern is a key or an exact detail. A key
	resolution hides entries with that key in every folder; a detail resolution
	hides entries whose detail matches the pattern exactly.
	"""
	_validate_category(category)
	if target not in RESOLUTION_TARGETS:
		raise ValueError("target must be detail or key")
	if target == "key":
		_validate_key(pattern)

	for field_name, value in (("pattern", pattern), ("reference", reference)):
		if not value.strip():
			raise ValueError(f"{field_name} must not be empty")

	resolved_at_utc = utc_timestamp()
	with database.transaction() as connection:
		cursor = connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference, target)
			VALUES (?, ?, ?, ?, ?)
			""",
			(category, pattern, resolved_at_utc, reference, target),
		)
		row = connection.execute(
			"""
			SELECT id, category, pattern, resolved_at_utc, reference, target
			FROM resolutions WHERE id = ?
			""",
			(cursor.lastrowid,),
		).fetchone()

	return dict(row)


def database_doctor(database: Database) -> dict[str, object]:
	"""Report database path, schema state, writability, and SQLite integrity."""
	database_path = database.path
	with database.connection() as connection:
		version = connection.execute(
			"SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
		).fetchone()[0]
		integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]

	return {
		"database_path": str(database_path),
		"schema_version": int(version),
		"database_writable": os.access(database_path, os.W_OK),
		"directory_writable": os.access(database_path.parent, os.W_OK),
		"integrity_check": integrity_check,
	}
