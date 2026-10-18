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
	"""Count active events by pattern, most frequent first.

	Keyed entries group by category and key across folders. Unkeyed tool errors
	group by command family and error class, and other unkeyed entries group by
	category and exact detail. Each row's example is the newest detail in the
	group, and its folders are the distinct working directories, sorted.
	"""
	category_conditions, parameters = _category_conditions(
		categories,
		include_check_fails,
		include_tool_errors,
	)
	conditions = [
		"""
		NOT EXISTS (
			SELECT 1 FROM resolutions
			WHERE resolutions.category = events.category
				AND resolutions.resolved_at_utc >= events.timestamp_utc
				AND (
					(resolutions.target = 'key' AND resolutions.pattern = events.key)
					OR (resolutions.target = 'detail' AND resolutions.pattern = events.detail)
				)
		)
		""",
		*category_conditions,
	]

	where_clause = " AND ".join(conditions)
	with database.connection() as connection:
		rows = connection.execute(
			f"""
			SELECT category, key, cwd, detail, tool_name, discriminator, error
			FROM events
			WHERE {where_clause}
			ORDER BY timestamp_utc DESC, id DESC
			""",
			parameters,
		).fetchall()

	return sorted(
		_group_events(rows),
		key=lambda item: (
			-item["count"],
			item["category"],
			item["key"] or item.get("command_family", item["example"]),
			item.get("error_class", ""),
		),
	)


def _group_events(rows: Sequence[sqlite3.Row]) -> list[dict[str, object]]:
	"""Count rows by pattern and collect each group's folders.

	Expects rows newest first, so each group's example is its most recent detail.
	"""
	groups: dict[tuple[str, str, str, str], dict[str, object]] = {}
	for row in rows:
		category = row["category"]
		pattern_key = row["key"]
		if pattern_key is not None:
			identity = (category, "key", pattern_key, "")
			extra = {}
		elif category == "tool-error":
			family, error_class = classify_tool_error(
				row["tool_name"], row["discriminator"], row["error"]
			)
			identity = (category, "tool-error", family, error_class)
			extra = {"command_family": family, "error_class": error_class}
		else:
			identity = (category, "detail", row["detail"], "")
			extra = {}

		if identity not in groups:
			group: dict[str, object] = {
				"count": 0,
				"category": category,
				"key": pattern_key,
				"example": row["detail"],
				"folders": set(),
				**extra,
			}
			groups[identity] = group

		groups[identity]["count"] += 1
		groups[identity]["folders"].add(row["cwd"])

	results = list(groups.values())
	for group in results:
		group["folders"] = sorted(group["folders"])

	return results


def list_events(
	database: Database,
	categories: Sequence[str] = (),
	include_check_fails: bool = False,
	include_tool_errors: bool = False,
	since: str | None = None,
	until: str | None = None,
) -> list[dict[str, object]]:
	"""Return matching event rows oldest first, including resolved events."""
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
				id,
				timestamp_utc,
				category,
				cwd,
				detail,
				key,
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
