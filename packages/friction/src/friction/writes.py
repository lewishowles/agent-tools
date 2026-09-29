"""Write friction events and resolution records."""

import os

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


def add_event(database: Database, category: str, detail: str) -> dict[str, object]:
	"""Store one manual event and return its complete persisted row."""
	_validate_category(category)

	if not detail.strip():
		raise ValueError("detail must not be empty")

	timestamp = utc_timestamp()
	cwd = os.getcwd()
	with database.transaction() as connection:
		cursor = connection.execute(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(timestamp, category, cwd, detail, "manual"),
		)
		row = connection.execute(
			"""
			SELECT id, timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			FROM events WHERE id = ?
			""",
			(cursor.lastrowid,),
		).fetchone()

	return dict(row)


def add_resolution(
	database: Database,
	category: str,
	pattern: str,
	reference: str,
) -> dict[str, object]:
	"""Store one category and pattern resolution without creating an event."""
	_validate_category(category)

	for field_name, value in (("pattern", pattern), ("reference", reference)):
		if not value.strip():
			raise ValueError(f"{field_name} must not be empty")

	resolved_at_utc = utc_timestamp()
	with database.transaction() as connection:
		cursor = connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference)
			VALUES (?, ?, ?, ?)
			""",
			(category, pattern, resolved_at_utc, reference),
		)
		row = connection.execute(
			"""
			SELECT id, category, pattern, resolved_at_utc, reference
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
