"""Read friction events from SQLite, either grouped (summary) or row by row (list)."""

import sqlite3
from collections.abc import Sequence

from .database import Database
from .tool_errors import classify_tool_error

# Categories that stay hidden unless a caller explicitly includes them.
AUTOMATED_CATEGORIES = frozenset({"check-fail", "tool-error"})


def _category_conditions(
	categories: Sequence[str],
	include_check_fails: bool,
	include_tool_errors: bool,
) -> tuple[list[str], list[str]]:
	"""Return the shared category filter clauses so summary and list never diverge."""
	conditions: list[str] = []
	parameters: list[str] = []
	if categories:
		placeholders = ", ".join("?" for _ in categories)
		conditions.append(f"events.category IN ({placeholders})")
		parameters.extend(categories)

	excluded_categories = set(AUTOMATED_CATEGORIES)
	if include_check_fails:
		excluded_categories.discard("check-fail")
	if include_tool_errors:
		excluded_categories.discard("tool-error")
	if excluded_categories:
		placeholders = ", ".join("?" for _ in excluded_categories)
		conditions.append(f"events.category NOT IN ({placeholders})")
		parameters.extend(sorted(excluded_categories))

	return conditions, parameters


def summary(
	database: Database,
	categories: Sequence[str] = (),
	include_check_fails: bool = False,
	include_tool_errors: bool = False,
) -> list[dict[str, object]]:
	"""Count active events by group, most frequent first.

	Tool errors group by command family and error class across directories, with the most recent entry's detail as the example. Every other category groups by category, directory, and exact detail.
	"""
	category_conditions, parameters = _category_conditions(
		categories,
		include_check_fails,
		include_tool_errors,
	)
	conditions = [
		"""
		NOT EXISTS (
			SELECT 1
			FROM resolutions
			WHERE resolutions.category = events.category
				AND resolutions.pattern = events.detail
				AND resolutions.resolved_at_utc >= events.timestamp_utc
		)
		""",
		*category_conditions,
	]

	where_clause = " AND ".join(conditions)
	with database.connection() as connection:
		rows = connection.execute(
			f"""
			SELECT
				COUNT(*) AS count,
				category,
				cwd,
				detail
			FROM events
			WHERE {where_clause} AND events.category != 'tool-error'
			GROUP BY category, cwd, detail
			""",
			parameters,
		).fetchall()
		tool_rows = connection.execute(
			f"""
			SELECT detail, tool_name, discriminator, error
			FROM events
			WHERE {where_clause} AND events.category = 'tool-error'
			ORDER BY timestamp_utc DESC, id DESC
			""",
			parameters,
		).fetchall()

	results = [dict(row) for row in rows] + _group_tool_errors(tool_rows)
	return sorted(
		results,
		key=lambda item: (
			-item["count"],
			item["category"],
			item.get("command_family", item.get("cwd")),
			item.get("error_class", item.get("detail")),
		),
	)


def _group_tool_errors(rows: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
	"""Count tool-error rows by command family and error class.

	Expects rows newest first, so each group's example is its most recent detail.
	"""
	groups: dict[tuple[str, str], dict[str, object]] = {}
	for row in rows:
		family, error_class = classify_tool_error(
			row["tool_name"], row["discriminator"], row["error"]
		)

		key = (family, error_class)
		if key not in groups:
			groups[key] = {
				"count": 0,
				"category": "tool-error",
				"command_family": family,
				"error_class": error_class,
				"example": row["detail"],
			}

		groups[key]["count"] += 1

	return list(groups.values())


def list_events(
	database: Database,
	categories: Sequence[str] = (),
	include_check_fails: bool = False,
	include_tool_errors: bool = False,
	since: str | None = None,
	until: str | None = None,
) -> list[dict[str, object]]:
	"""Return matching event rows oldest first.

	Unlike summary, an event stays listed after a resolution hides it.
	"""
	conditions, parameters = _category_conditions(
		categories,
		include_check_fails,
		include_tool_errors,
	)
	if since is not None:
		conditions.append("events.timestamp_utc >= ?")
		parameters.append(since)
	if until is not None:
		conditions.append("substr(events.timestamp_utc, 1, 10) <= ?")
		parameters.append(until)

	where_clause = " AND ".join(conditions)
	where_statement = f"WHERE {where_clause}" if where_clause else ""
	with database.connection() as connection:
		rows = connection.execute(
			f"""
			SELECT
				timestamp_utc,
				category,
				cwd,
				detail,
				source,
				tool_name,
				discriminator,
				error
			FROM events
			{where_statement}
			ORDER BY timestamp_utc ASC, id ASC
			""",
			parameters,
		).fetchall()

	return [dict(row) for row in rows]
