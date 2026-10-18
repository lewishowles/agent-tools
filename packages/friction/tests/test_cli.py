"""Tests for friction commands, imports, hooks, and database setup."""

import json
import sqlite3
import stat
from io import StringIO

import pytest

from friction import cli
from friction.database import (
	BUSY_TIMEOUT_SECONDS,
	DATABASE_ENVIRONMENT_VARIABLE,
	Database,
	resolve_database_path,
)
from friction.errors import DatabaseBusyError


class FailingStdin:
	"""Mimic a hook input stream that cannot be decoded."""

	def read(self) -> str:
		"""Raise the decoding failure that a broken stdin stream can produce."""
		raise UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "invalid start byte")


def test_add_stores_a_manual_event_and_returns_its_json_row(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"

	exit_code = cli.main(
		[
			"add",
			"rule-ignored",
			"skipped the review gate",
			"--database",
			str(database_path),
			"--json",
		]
	)
	captured = capsys.readouterr()
	response = json.loads(captured.out)

	assert exit_code == 0
	assert captured.err == ""
	assert response["ok"] is True
	assert response["data"]["category"] == "rule-ignored"
	assert response["data"]["detail"] == "skipped the review gate"
	assert response["data"]["key"] is None
	assert response["data"]["source"] == "manual"

	with Database(database_path).connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_add_stores_a_key_on_a_manual_event(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"

	exit_code = cli.main(
		[
			"add",
			"rule-ignored",
			"skipped the review gate",
			"--key",
			"review-gate-2",
			"--database",
			str(database_path),
			"--json",
		]
	)
	response = json.loads(capsys.readouterr().out)

	assert exit_code == 0
	assert response["data"]["key"] == "review-gate-2"

	with Database(database_path).connection() as connection:
		assert (
			connection.execute("SELECT key FROM events").fetchone()[0]
			== "review-gate-2"
		)


@pytest.mark.parametrize(
	"key", ["Review-gate", "review_gate", "-review", "review--gate", "review-"]
)
def test_add_rejects_a_malformed_key_without_creating_a_database(
	tmp_path, capsys, key
) -> None:
	database_path = tmp_path / "friction.db"

	exit_code = cli.main(
		[
			"add",
			"rule-ignored",
			"skipped the review gate",
			f"--key={key}",
			"--database",
			str(database_path),
			"--json",
		]
	)
	response = json.loads(capsys.readouterr().out)

	assert exit_code == 1
	assert response == {
		"ok": False,
		"error": {"message": "key must be lowercase kebab-case (letters and digits)"},
	}
	assert not database_path.exists()


def test_add_rejects_an_unknown_category_without_creating_a_database(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"

	exit_code = cli.main(["add", "unknown", "detail", "--database", str(database_path)])
	captured = capsys.readouterr()

	assert exit_code == 2
	assert captured.out == ""
	assert "invalid choice" in captured.err
	assert not database_path.exists()


def test_resolve_creates_no_event_and_returns_its_json_row(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"

	exit_code = cli.main(
		[
			"resolve",
			"wrong-approach",
			"skipped investigation",
			"--reference",
			"fix: require investigation",
			"--database",
			str(database_path),
			"--json",
		]
	)
	captured = capsys.readouterr()
	response = json.loads(captured.out)

	assert exit_code == 0
	assert captured.err == ""
	assert response["data"]["reference"] == "fix: require investigation"
	assert response["data"]["target"] == "detail"

	with Database(database_path).connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
		assert connection.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0] == 1


def test_import_handles_current_legacy_and_resolution_rows_idempotently(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"
	source_path = tmp_path / "friction.log"
	source_path.write_text(
		"2026-09-03\trule-ignored\t/workspace\tcurrent row\n"
		"2026-09-03\t/workspace\tlegacy check\t\n"
		"RESOLVED\trule-ignored\tcurrent row\tfix: review\n"
		"invalid\n"
	)

	assert (
		cli.main(
			["import", str(source_path), "--database", str(database_path), "--json"]
		)
		== 0
	)
	first = json.loads(capsys.readouterr().out)["data"]
	assert first["imported"] == 3
	assert first["skipped_as_duplicate"] == 0
	assert first["rejected"] == 1
	assert first["rejected_rows"] == [
		{
			"source_path": str(source_path.resolve()),
			"source_line": 4,
			"reason": "event rows need four columns",
		}
	]

	assert (
		cli.main(
			["import", str(source_path), "--database", str(database_path), "--json"]
		)
		== 0
	)
	second = json.loads(capsys.readouterr().out)["data"]
	assert second["imported"] == 0
	assert second["skipped_as_duplicate"] == 3
	assert second["rejected"] == 1

	with Database(database_path).connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
		assert connection.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0] == 1
		assert (
			connection.execute(
				"SELECT detail FROM events WHERE category = 'check-fail'"
			).fetchone()[0]
			== "legacy check"
		)


def test_import_dry_run_does_not_write_events_or_provenance(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"
	source_path = tmp_path / "friction.log"
	source_path.write_text("2026-09-03\trule-ignored\t/workspace\tdry run\n")

	assert (
		cli.main(
			[
				"import",
				str(source_path),
				"--dry-run",
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 0
	)
	response = json.loads(capsys.readouterr().out)["data"]
	assert response["imported"] == 1

	with Database(database_path).connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
		assert (
			connection.execute("SELECT COUNT(*) FROM import_provenance").fetchone()[0]
			== 0
		)


def test_summary_hides_events_at_or_before_resolution(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(
				(
					"2026-09-03T09:00:00+00:00",
					"rule-ignored",
					"/workspace",
					"skipped review",
					"manual",
				),
				(
					"2026-09-03T09:30:00+00:00",
					"rule-ignored",
					"/workspace",
					"skipped review",
					"manual",
				),
				(
					"2026-09-03T10:00:00+00:00",
					"rule-ignored",
					"/workspace",
					"skipped review",
					"manual",
				),
				(
					"2026-09-03T11:00:00+00:00",
					"rule-ignored",
					"/workspace",
					"skipped review",
					"manual",
				),
				(
					"2026-09-03T12:00:00+00:00",
					"wrong-approach",
					"/workspace",
					"skipped investigation",
					"manual",
				),
			),
		)
		connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference)
			VALUES (?, ?, ?, ?)
			""",
			(
				"rule-ignored",
				"skipped review",
				"2026-09-03T10:00:00+00:00",
				"fix: require review",
			),
		)

	exit_code = cli.main(["summary", "--database", str(database_path), "--json"])
	captured = capsys.readouterr()
	response = json.loads(captured.out)

	assert exit_code == 0
	assert captured.err == ""
	assert response == {
		"ok": True,
		"data": [
			{
				"count": 1,
				"category": "rule-ignored",
				"key": None,
				"example": "skipped review",
				"folders": ["/workspace"],
			},
			{
				"count": 1,
				"category": "wrong-approach",
				"key": None,
				"example": "skipped investigation",
				"folders": ["/workspace"],
			},
		],
	}


def test_summary_shows_event_logged_after_resolution(
	tmp_path, capsys, monkeypatch
) -> None:
	database_path = tmp_path / "friction.db"
	monkeypatch.chdir(tmp_path)

	assert (
		cli.main(
			[
				"resolve",
				"rule-ignored",
				"skipped review",
				"--reference",
				"fix: require review",
				"--database",
				str(database_path),
			]
		)
		== 0
	)
	capsys.readouterr()

	assert (
		cli.main(
			[
				"add",
				"rule-ignored",
				"skipped review",
				"--database",
				str(database_path),
			]
		)
		== 0
	)
	capsys.readouterr()

	exit_code = cli.main(["summary", "--database", str(database_path), "--json"])
	captured = capsys.readouterr()
	response = json.loads(captured.out)

	assert exit_code == 0
	assert captured.err == ""
	assert response == {
		"ok": True,
		"data": [
			{
				"count": 1,
				"category": "rule-ignored",
				"key": None,
				"example": "skipped review",
				"folders": [str(tmp_path)],
			}
		],
	}


def test_summary_excludes_automated_categories_until_their_flags_are_passed(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(
				(
					"2026-09-03",
					"rule-ignored",
					"/workspace",
					"manual entry",
					"manual",
				),
				(
					"2026-09-03",
					"check-fail",
					"/workspace",
					"verification failed",
					"manual",
				),
				(
					"2026-09-03",
					"tool-error",
					"/workspace",
					"tool invocation failed",
					"manual",
				),
			),
		)

	assert cli.main(["summary", "--database", str(database_path), "--json"]) == 0
	default_response = json.loads(capsys.readouterr().out)
	assert [item["category"] for item in default_response["data"]] == ["rule-ignored"]

	assert (
		cli.main(
			[
				"summary",
				"--include-check-fails",
				"--include-tool-errors",
				"--category",
				"check-fail",
				"--category",
				"tool-error",
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 0
	)
	filtered_response = json.loads(capsys.readouterr().out)

	assert [item["category"] for item in filtered_response["data"]] == [
		"check-fail",
		"tool-error",
	]


def test_summary_groups_tool_errors_across_directories_and_keeps_other_rows(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				(
					"2026-09-03T09:00:00+00:00",
					"tool-error",
					"/one",
					"first git failure",
					"claude-hook",
					"Bash",
					"git diff",
					"Exit code 1",
				),
				(
					"2026-09-03T10:00:00+00:00",
					"tool-error",
					"/two",
					"latest git failure",
					"claude-hook",
					"Bash",
					"git status",
					"Exit code 1",
				),
				(
					"2026-09-03T11:00:00+00:00",
					"tool-error",
					"/three",
					"resolved git failure",
					"claude-hook",
					"Bash",
					"git log",
					"Exit code 1",
				),
				(
					"2026-09-03T09:00:00+00:00",
					"tool-error",
					"/one",
					"missing fields",
					"manual",
					None,
					None,
					None,
				),
				(
					"2026-09-03T09:00:00+00:00",
					"rule-ignored",
					"/one",
					"same detail",
					"manual",
					None,
					None,
					None,
				),
				(
					"2026-09-03T09:00:00+00:00",
					"rule-ignored",
					"/two",
					"same detail",
					"manual",
					None,
					None,
					None,
				),
				(
					"2026-09-03T09:00:00+00:00",
					"check-fail",
					"/one",
					"check failed",
					"manual",
					None,
					None,
					None,
				),
			),
		)
		connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference)
			VALUES (?, ?, ?, ?)
			""",
			(
				"tool-error",
				"resolved git failure",
				"2026-09-03T12:00:00+00:00",
				"fixed",
			),
		)

	assert (
		cli.main(
			[
				"summary",
				"--include-tool-errors",
				"--include-check-fails",
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 0
	)
	response = json.loads(capsys.readouterr().out)

	assert response == {
		"ok": True,
		"data": [
			{
				"count": 2,
				"category": "rule-ignored",
				"key": None,
				"example": "same detail",
				"folders": ["/one", "/two"],
			},
			{
				"count": 2,
				"category": "tool-error",
				"key": None,
				"command_family": "git",
				"error_class": "exit 1",
				"example": "latest git failure",
				"folders": ["/one", "/two"],
			},
			{
				"count": 1,
				"category": "check-fail",
				"key": None,
				"example": "check failed",
				"folders": ["/one"],
			},
			{
				"count": 1,
				"category": "tool-error",
				"key": None,
				"command_family": "unknown",
				"error_class": "unknown",
				"example": "missing fields",
				"folders": ["/one"],
			},
		],
	}

	assert (
		cli.main(
			[
				"summary",
				"--include-tool-errors",
				"--include-check-fails",
				"--database",
				str(database_path),
			]
		)
		== 0
	)
	human_output = capsys.readouterr().out
	assert "2  rule-ignored  (latest: same detail)" in human_output
	assert "  folders: /one, /two" in human_output

	assert (
		cli.main(
			[
				"summary",
				"--include-tool-errors",
				"--category",
				"tool-error",
				"--database",
				str(database_path),
			]
		)
		== 0
	)
	human_output = capsys.readouterr().out
	assert "2  tool-error  git  exit 1  (latest: latest git failure)" in human_output
	assert "1  tool-error  unknown  unknown  (latest: missing fields)" in human_output
	assert "rule-ignored" not in human_output


def test_summary_orders_equal_count_tool_errors_by_family_and_class(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, 'tool-error', '/workspace', ?, 'claude-hook', ?, ?, ?)
			""",
			(
				("2026-09-03", "third", "Bash", "git log", "Exit code 3"),
				("2026-09-03", "second", "Bash", "git diff", "Exit code 2"),
				("2026-09-03", "first", "Read", "/workspace/file", "File missing"),
			),
		)

	assert (
		cli.main(
			[
				"summary",
				"--include-tool-errors",
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 0
	)
	rows = json.loads(capsys.readouterr().out)["data"]

	assert [(row["command_family"], row["error_class"]) for row in rows] == [
		("Read", "File missing"),
		("git", "exit 2"),
		("git", "exit 3"),
	]


def test_summary_groups_keyed_entries_across_folders_and_tool_error_classes(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source, key,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, 'manual', ?, ?, ?, ?)
			""",
			[
				(
					"2026-09-03T09:00:00+00:00",
					"rule-ignored",
					"/b",
					"first",
					"review-gate",
					None,
					None,
					None,
				),
				(
					"2026-09-03T10:00:00+00:00",
					"rule-ignored",
					"/a",
					"latest",
					"review-gate",
					None,
					None,
					None,
				),
				(
					"2026-09-03T11:00:00+00:00",
					"rule-ignored",
					"/a",
					"first",
					None,
					None,
					None,
					None,
				),
				(
					"2026-09-03T12:00:00+00:00",
					"tool-error",
					"/b",
					"git failed",
					"retry-tool",
					"Bash",
					"git diff",
					"Exit code 1",
				),
				(
					"2026-09-03T13:00:00+00:00",
					"tool-error",
					"/a",
					"read failed",
					"retry-tool",
					"Read",
					"/a/file",
					"File missing",
				),
			],
		)

	assert (
		cli.main(
			[
				"summary",
				"--include-tool-errors",
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 0
	)
	rows = json.loads(capsys.readouterr().out)["data"]

	assert rows == [
		{
			"count": 2,
			"category": "rule-ignored",
			"key": "review-gate",
			"example": "latest",
			"folders": ["/a", "/b"],
		},
		{
			"count": 2,
			"category": "tool-error",
			"key": "retry-tool",
			"example": "read failed",
			"folders": ["/a", "/b"],
		},
		{
			"count": 1,
			"category": "rule-ignored",
			"key": None,
			"example": "first",
			"folders": ["/a"],
		},
	]

	assert (
		cli.main(["summary", "--include-tool-errors", "--database", str(database_path)])
		== 0
	)
	human_output = capsys.readouterr().out
	assert (
		"2  rule-ignored  review-gate  (latest: latest)\n  folders: /a, /b"
		in human_output
	)
	assert (
		"2  tool-error  retry-tool  (latest: read failed)\n  folders: /a, /b"
		in human_output
	)


def test_key_and_detail_resolutions_hide_only_their_own_earlier_matches(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (timestamp_utc, category, cwd, detail, source, key)
			VALUES (?, 'rule-ignored', ?, ?, 'manual', ?)
			""",
			[
				("2026-09-03T09:00:00+00:00", "/a", "first wording", "review-gate"),
				("2026-09-03T09:30:00+00:00", "/b", "second wording", "review-gate"),
				("2026-09-03T09:45:00+00:00", "/c", "review-gate", None),
				("2026-09-03T10:30:00+00:00", "/d", "later wording", "review-gate"),
			],
		)
		connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference, target)
			VALUES ('rule-ignored', 'review-gate', '2026-09-03T10:00:00+00:00', 'fix', 'key')
			"""
		)

	assert cli.main(["summary", "--database", str(database_path), "--json"]) == 0
	rows = json.loads(capsys.readouterr().out)["data"]
	assert [row["example"] for row in rows] == ["later wording", "review-gate"]

	with Database(database_path).transaction() as connection:
		connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference, target)
			VALUES ('rule-ignored', 'review-gate', '2026-09-03T11:00:00+00:00', 'fix', 'detail')
			"""
		)

	assert cli.main(["summary", "--database", str(database_path), "--json"]) == 0
	rows = json.loads(capsys.readouterr().out)["data"]
	assert [row["example"] for row in rows] == ["later wording"]


def test_resolve_requires_either_a_detail_or_a_valid_key(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"
	base = [
		"resolve",
		"rule-ignored",
		"--reference",
		"fix",
		"--database",
		str(database_path),
		"--json",
	]

	for extra in ([], ["detail", "--key", "review-gate"]):
		assert cli.main([*base, *extra]) == 2
		assert "exactly one" in json.loads(capsys.readouterr().out)["error"]["message"]

	assert cli.main([*base, "--key", "Review-gate"]) == 1
	assert (
		"lowercase kebab-case"
		in json.loads(capsys.readouterr().out)["error"]["message"]
	)
	assert not database_path.exists()

	assert cli.main([*base, "--key", "review-gate"]) == 0
	assert json.loads(capsys.readouterr().out)["data"]["target"] == "key"


def test_list_returns_the_raw_event_envelope_in_timestamp_order(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				(
					"2026-09-03T11:00:00+00:00",
					"rule-ignored",
					"/workspace",
					"second event",
					"manual",
					None,
					None,
					None,
				),
				(
					"2026-09-03T10:00:00+00:00",
					"wrong-approach",
					"/workspace",
					"first event",
					"claude-hook",
					"Bash",
					"run checks",
					"failed",
				),
			),
		)

	exit_code = cli.main(
		[
			"list",
			"--include-check-fails",
			"--include-tool-errors",
			"--database",
			str(database_path),
			"--json",
		]
	)
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.err == ""
	assert json.loads(captured.out) == {
		"ok": True,
		"data": [
			{
				"id": 2,
				"timestamp_utc": "2026-09-03T10:00:00+00:00",
				"category": "wrong-approach",
				"cwd": "/workspace",
				"detail": "first event",
				"key": None,
				"source": "claude-hook",
				"tool_name": "Bash",
				"discriminator": "run checks",
				"error": "failed",
			},
			{
				"id": 1,
				"timestamp_utc": "2026-09-03T11:00:00+00:00",
				"category": "rule-ignored",
				"cwd": "/workspace",
				"detail": "second event",
				"key": None,
				"source": "manual",
				"tool_name": None,
				"discriminator": None,
				"error": None,
			},
		],
	}


def test_list_uses_the_same_category_filters_as_summary(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(
				("2026-09-03", "rule-ignored", "/workspace", "manual", "manual"),
				("2026-09-03", "check-fail", "/workspace", "check", "manual"),
				("2026-09-03", "tool-error", "/workspace", "tool", "manual"),
			),
		)

	assert cli.main(["summary", "--database", str(database_path), "--json"]) == 0
	summary_categories = [
		item["category"] for item in json.loads(capsys.readouterr().out)["data"]
	]
	assert cli.main(["list", "--database", str(database_path), "--json"]) == 0
	list_categories = [
		item["category"] for item in json.loads(capsys.readouterr().out)["data"]
	]

	assert set(list_categories) == set(summary_categories) == {"rule-ignored"}

	arguments = [
		"--include-check-fails",
		"--include-tool-errors",
		"--database",
		str(database_path),
		"--json",
	]
	assert cli.main(["summary", *arguments]) == 0
	summary_categories = [
		item["category"] for item in json.loads(capsys.readouterr().out)["data"]
	]
	assert cli.main(["list", *arguments]) == 0
	list_categories = [
		item["category"] for item in json.loads(capsys.readouterr().out)["data"]
	]

	assert (
		set(list_categories)
		== set(summary_categories)
		== {
			"check-fail",
			"rule-ignored",
			"tool-error",
		}
	)


def test_list_filters_events_with_inclusive_date_boundaries(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.executemany(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(
				(
					"2026-09-02T23:59:59+00:00",
					"rule-ignored",
					"/workspace",
					"before",
					"manual",
				),
				(
					"2026-09-03T00:00:00+00:00",
					"rule-ignored",
					"/workspace",
					"start",
					"manual",
				),
				(
					"2026-09-03T23:59:59+00:00",
					"rule-ignored",
					"/workspace",
					"end",
					"manual",
				),
				(
					"2026-09-04T00:00:00+00:00",
					"rule-ignored",
					"/workspace",
					"after",
					"manual",
				),
			),
		)

	exit_code = cli.main(
		[
			"list",
			"--since",
			"2026-09-03",
			"--until",
			"2026-09-03",
			"--database",
			str(database_path),
			"--json",
		]
	)
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.err == ""
	assert [item["detail"] for item in json.loads(captured.out)["data"]] == [
		"start",
		"end",
	]


def test_list_returns_events_hidden_by_a_resolution(tmp_path, capsys) -> None:
	database_path = tmp_path / "friction.db"
	with Database(database_path).transaction() as connection:
		connection.execute(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(
				"2026-09-03T09:00:00+00:00",
				"rule-ignored",
				"/workspace",
				"resolved",
				"manual",
			),
		)
		connection.execute(
			"""
			INSERT INTO events (timestamp_utc, category, cwd, detail, source, key)
			VALUES ('2026-09-03T09:30:00+00:00', 'rule-ignored', '/workspace',
				'key resolved', 'manual', 'review-gate')
			"""
		)
		connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference)
			VALUES (?, ?, ?, ?)
			""",
			("rule-ignored", "resolved", "2026-09-03T10:00:00+00:00", "fix"),
		)
		connection.execute(
			"""
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference, target)
			VALUES ('rule-ignored', 'review-gate', '2026-09-03T10:00:00+00:00', 'fix', 'key')
			"""
		)

	assert cli.main(["list", "--database", str(database_path), "--json"]) == 0

	assert [item["detail"] for item in json.loads(capsys.readouterr().out)["data"]] == [
		"resolved",
		"key resolved",
	]


def test_list_rejects_an_invalid_date_as_a_json_usage_error(tmp_path, capsys) -> None:
	exit_code = cli.main(
		[
			"list",
			"--since",
			"2026-9-3",
			"--database",
			str(tmp_path / "friction.db"),
			"--json",
		]
	)
	captured = capsys.readouterr()

	assert exit_code == 2
	assert captured.err == ""
	assert json.loads(captured.out) == {
		"ok": False,
		"error": {"message": "argument --since: expected YYYY-MM-DD"},
	}


def test_list_shows_an_empty_human_result(tmp_path, capsys) -> None:
	exit_code = cli.main(["list", "--database", str(tmp_path / "friction.db")])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.err == ""
	assert "No friction entries." in captured.out


def test_list_shows_each_event_on_one_human_row(tmp_path, capsys, monkeypatch) -> None:
	database_path = tmp_path / "friction.db"
	monkeypatch.setattr(cli.style, "table", cli.style._plain_table)
	with Database(database_path).transaction() as connection:
		connection.execute(
			"""
			INSERT INTO events (
				timestamp_utc, category, cwd, detail, source, key,
				tool_name, discriminator, error
			) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
			""",
			(
				"2026-09-03T09:41:00+00:00",
				"wrong-approach",
				"/workspace",
				"tried a manual retry loop",
				"manual",
				"manual-retry-loop",
			),
		)

	exit_code = cli.main(["list", "--database", str(database_path)])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.err == ""
	assert "2026-09-03T09:41:00+00:00" in captured.out
	row = next(
		line for line in captured.out.splitlines() if "manual-retry-loop" in line
	)
	assert row.split()[0] == "1"
	assert row.split()[3] == "manual-retry-loop"
	assert "wrong-approach" in captured.out
	assert "tried a manual retry loop" in captured.out


def test_doctor_reports_database_health_and_private_database_permissions(
	tmp_path, capsys
) -> None:
	database_path = tmp_path / "nested" / "friction.db"

	exit_code = cli.main(["doctor", "--database", str(database_path), "--json"])
	captured = capsys.readouterr()
	response = json.loads(captured.out)

	assert exit_code == 0
	assert captured.err == ""
	assert response == {
		"ok": True,
		"data": {
			"database_path": str(database_path),
			"schema_version": 4,
			"database_writable": True,
			"directory_writable": True,
			"integrity_check": "ok",
		},
	}
	assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


def test_database_connection_configures_wal_and_busy_timeout(tmp_path) -> None:
	database_path = tmp_path / "friction.db"

	with Database(database_path).connection() as connection:
		assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
		assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == int(
			BUSY_TIMEOUT_SECONDS * 1000
		)


def test_database_migration_removes_the_redundant_runtime_column(tmp_path) -> None:
	database_path = tmp_path / "friction.db"
	connection = sqlite3.connect(database_path)
	connection.execute(
		"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
	)
	connection.execute(
		"INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-09-03')"
	)
	connection.execute(
		"""
		CREATE TABLE events (
			id INTEGER PRIMARY KEY,
			timestamp_utc TEXT NOT NULL,
			category TEXT NOT NULL,
			cwd TEXT NOT NULL,
			detail TEXT NOT NULL,
			source TEXT NOT NULL,
			runtime TEXT NOT NULL,
			tool_name TEXT,
			discriminator TEXT,
			error TEXT
		)
		"""
	)
	connection.execute(
		"""
		INSERT INTO events (
			timestamp_utc, category, cwd, detail, source, runtime,
			tool_name, discriminator, error
		) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
		""",
		("2026-09-03", "rule-ignored", "/workspace", "detail", "manual", "codex"),
	)
	connection.execute(
		"""
		CREATE TABLE resolutions (
			id INTEGER PRIMARY KEY,
			category TEXT NOT NULL,
			pattern TEXT NOT NULL,
			resolved_at_utc TEXT NOT NULL,
			reference TEXT NOT NULL
		)
		"""
	)
	connection.commit()
	connection.close()

	with Database(database_path).connection() as migrated_connection:
		columns = {
			row["name"]
			for row in migrated_connection.execute(
				"PRAGMA table_info(events)"
			).fetchall()
		}
		row = migrated_connection.execute(
			"SELECT category, source FROM events"
		).fetchone()

	assert "runtime" not in columns
	assert dict(row) == {"category": "rule-ignored", "source": "manual"}


def test_database_migration_adds_nullable_keys_to_version_2_events(tmp_path) -> None:
	database_path = tmp_path / "friction.db"
	connection = sqlite3.connect(database_path)
	connection.execute(
		"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
	)
	connection.executemany(
		"INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-09-03')",
		[(1,), (2,)],
	)
	connection.execute(
		"""
		CREATE TABLE events (
			id INTEGER PRIMARY KEY,
			timestamp_utc TEXT NOT NULL,
			category TEXT NOT NULL,
			cwd TEXT NOT NULL,
			detail TEXT NOT NULL,
			source TEXT NOT NULL,
			tool_name TEXT,
			discriminator TEXT,
			error TEXT
		)
		"""
	)
	connection.execute(
		"""
		INSERT INTO events (id, timestamp_utc, category, cwd, detail, source)
		VALUES (17, '2026-09-03', 'rule-ignored', '/workspace', 'detail', 'manual')
		"""
	)
	connection.execute(
		"""
		CREATE TABLE resolutions (
			id INTEGER PRIMARY KEY,
			category TEXT NOT NULL,
			pattern TEXT NOT NULL,
			resolved_at_utc TEXT NOT NULL,
			reference TEXT NOT NULL
		)
		"""
	)
	connection.commit()
	connection.close()

	with Database(database_path).connection() as migrated_connection:
		row = migrated_connection.execute(
			"SELECT id, detail, key FROM events"
		).fetchone()
		version = migrated_connection.execute(
			"SELECT MAX(version) FROM schema_migrations"
		).fetchone()[0]

	assert dict(row) == {"id": 17, "detail": "detail", "key": None}
	assert version == 4


def test_database_migration_marks_version_3_resolutions_as_detail(tmp_path) -> None:
	database_path = tmp_path / "friction.db"
	connection = sqlite3.connect(database_path)
	connection.execute(
		"CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
	)
	connection.executemany(
		"INSERT INTO schema_migrations (version, applied_at) VALUES (?, '2026-09-03')",
		[(1,), (2,), (3,)],
	)
	connection.execute(
		"""
		CREATE TABLE resolutions (
			id INTEGER PRIMARY KEY,
			category TEXT NOT NULL,
			pattern TEXT NOT NULL,
			resolved_at_utc TEXT NOT NULL,
			reference TEXT NOT NULL
		)
		"""
	)
	connection.execute(
		"""
		INSERT INTO resolutions (category, pattern, resolved_at_utc, reference)
		VALUES ('rule-ignored', 'review-gate', '2026-09-03', 'fix')
		"""
	)
	connection.commit()
	connection.close()

	with Database(database_path).connection() as migrated_connection:
		row = migrated_connection.execute(
			"SELECT pattern, target FROM resolutions"
		).fetchone()
		version = migrated_connection.execute(
			"SELECT MAX(version) FROM schema_migrations"
		).fetchone()[0]

	assert dict(row) == {"pattern": "review-gate", "target": "detail"}
	assert version == 4


def test_resolve_database_path_uses_the_environment_override(
	tmp_path, monkeypatch
) -> None:
	database_path = tmp_path / "environment.db"
	monkeypatch.setenv(DATABASE_ENVIRONMENT_VARIABLE, str(database_path))

	assert resolve_database_path() == database_path


def test_main_formats_a_busy_database_error_without_a_traceback(
	monkeypatch, capsys
) -> None:
	def raise_busy_error(args) -> tuple[dict[str, object], str]:
		raise DatabaseBusyError(
			"database remained locked for the five-second retry window"
		)

	monkeypatch.setattr(cli, "_run_command", raise_busy_error)

	exit_code = cli.main(["doctor", "--json"])
	captured = capsys.readouterr()
	response = json.loads(captured.out)

	assert exit_code == 1
	assert captured.err == ""
	assert response == {
		"ok": False,
		"error": {
			"message": "database remained locked for the five-second retry window"
		},
	}


def test_claude_tool_failure_hook_records_sanitised_fields(
	tmp_path, monkeypatch
) -> None:
	database_path = tmp_path / "friction.db"
	monkeypatch.setattr(
		"sys.stdin",
		StringIO(
			json.dumps(
				{
					"tool_name": "Bash\n",
					"tool_input": {"command": "run\tchecks"},
					"error": "failed\rnow",
				}
			)
		),
	)
	assert (
		cli.main(["hook", "claude-tool-failure", "--database", str(database_path)]) == 0
	)
	with Database(database_path).connection() as connection:
		row = connection.execute(
			"SELECT category, source, tool_name, discriminator, error, detail FROM events"
		).fetchone()
	assert dict(row) == {
		"category": "tool-error",
		"source": "claude-hook",
		"tool_name": "Bash ",
		"discriminator": "run checks",
		"error": "failed now",
		"detail": "Bash : run checks — failed now",
	}


def test_claude_tool_failure_hook_ignores_malformed_input(
	tmp_path, monkeypatch
) -> None:
	database_path = tmp_path / "friction.db"
	monkeypatch.setattr("sys.stdin", StringIO("not json"))
	assert (
		cli.main(["hook", "claude-tool-failure", "--database", str(database_path)]) == 0
	)
	assert not database_path.exists()


def test_claude_tool_failure_hook_ignores_empty_input(tmp_path, monkeypatch) -> None:
	database_path = tmp_path / "friction.db"
	monkeypatch.setattr("sys.stdin", StringIO(""))

	assert (
		cli.main(["hook", "claude-tool-failure", "--database", str(database_path)]) == 0
	)
	assert not database_path.exists()


def test_claude_tool_failure_hook_ignores_an_unreadable_stdin_stream(
	tmp_path, monkeypatch
) -> None:
	database_path = tmp_path / "friction.db"
	monkeypatch.setattr("sys.stdin", FailingStdin())

	assert (
		cli.main(["hook", "claude-tool-failure", "--database", str(database_path)]) == 0
	)
	assert not database_path.exists()
