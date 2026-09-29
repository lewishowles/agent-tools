"""Import legacy friction TSV rows without changing their source files."""

from pathlib import Path

from .database import Database
from .schema import utc_timestamp
from .writes import CATEGORIES


def _parse_row(fields: list[str]) -> tuple[str, dict[str, str]] | str:
	"""Parse one current, legacy, or resolution row, or return its rejection reason."""
	if fields[0] == "RESOLVED":
		if len(fields) != 4 or not all(fields[1:]):
			return "RESOLVED rows need category, pattern, and reference"
		if fields[1] not in CATEGORIES:
			return f"unknown category {fields[1]!r}"
		return "resolution", {
			"category": fields[1],
			"pattern": fields[2],
			"reference": fields[3],
		}

	if len(fields) != 4:
		return "event rows need four columns"
	if fields[1] in CATEGORIES:
		if not all(fields):
			return "current event rows need four non-empty columns"
		return "event", {
			"timestamp_utc": fields[0],
			"category": fields[1],
			"cwd": fields[2],
			"detail": fields[3],
		}

	if not all(fields[:3]):
		return "legacy event rows need a timestamp, directory, and failed checks"

	legacy_detail_fields = list(fields[2:])
	while legacy_detail_fields and not legacy_detail_fields[-1]:
		legacy_detail_fields.pop()

	return "event", {
		"timestamp_utc": fields[0],
		"category": "check-fail",
		"cwd": fields[1],
		"detail": "\t".join(legacy_detail_fields),
	}


def import_files(
	database: Database,
	paths: list[str],
	*,
	dry_run: bool = False,
) -> dict[str, object]:
	"""Import rows from paths and return counts plus rejected-line reasons."""
	sources = [(Path(path).resolve(), Path(path).read_text()) for path in paths]
	result: dict[str, object] = {
		"imported": 0,
		"skipped_as_duplicate": 0,
		"rejected": 0,
		"rejected_rows": [],
	}

	for source_path, contents in sources:
		context = database.connection() if dry_run else database.transaction()
		with context as connection:
			for source_line, raw_line in enumerate(contents.splitlines(), start=1):
				parsed = _parse_row(raw_line.split("\t")) if raw_line else "empty row"
				if isinstance(parsed, str):
					result["rejected"] = int(result["rejected"]) + 1
					result["rejected_rows"].append(
						{
							"source_path": str(source_path),
							"source_line": source_line,
							"reason": parsed,
						}
					)
					continue

				kind, values = parsed
				provenance = connection.execute(
					"SELECT 1 FROM import_provenance WHERE source_path = ? AND source_line = ?",
					(str(source_path), source_line),
				).fetchone()
				if provenance is not None:
					result["skipped_as_duplicate"] = (
						int(result["skipped_as_duplicate"]) + 1
					)
					continue

				result["imported"] = int(result["imported"]) + 1
				if dry_run:
					continue

				if kind == "event":
					source = (
						"claude-hook"
						if values["category"] == "tool-error"
						else "manual"
					)
					connection.execute(
						"INSERT INTO events (timestamp_utc, category, cwd, detail, source) VALUES (?, ?, ?, ?, ?)",
						(
							values["timestamp_utc"],
							values["category"],
							values["cwd"],
							values["detail"],
							source,
						),
					)
				else:
					connection.execute(
						"INSERT INTO resolutions (category, pattern, resolved_at_utc, reference) VALUES (?, ?, ?, ?)",
						(
							values["category"],
							values["pattern"],
							utc_timestamp(),
							values["reference"],
						),
					)

				connection.execute(
					"INSERT INTO import_provenance (source_path, source_line, imported_at_utc) VALUES (?, ?, ?)",
					(str(source_path), source_line, utc_timestamp()),
				)

	return result
