"""Read friction events from SQLite, either grouped (summary) or row by row (list)."""

from collections.abc import Sequence

from .database import Database

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
	"""Return active events grouped by category, directory, and exact detail."""
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
			WHERE {where_clause}
			GROUP BY category, cwd, detail
			ORDER BY count DESC, category, cwd, detail
			""",
			parameters,
		).fetchall()

	return [dict(row) for row in rows]


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
