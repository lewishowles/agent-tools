"""Tests for friction commands, imports, hooks, and database setup."""

import json
import sqlite3
import stat
from io import StringIO

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
	assert response["data"]["source"] == "manual"

	with Database(database_path).connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


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
				"cwd": "/workspace",
				"detail": "skipped review",
			},
			{
				"count": 1,
				"category": "wrong-approach",
				"cwd": "/workspace",
				"detail": "skipped investigation",
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
				"cwd": str(tmp_path),
				"detail": "skipped review",
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
				"timestamp_utc": "2026-09-03T10:00:00+00:00",
				"category": "wrong-approach",
				"cwd": "/workspace",
				"detail": "first event",
				"source": "claude-hook",
				"tool_name": "Bash",
				"discriminator": "run checks",
				"error": "failed",
			},
			{
				"timestamp_utc": "2026-09-03T11:00:00+00:00",
				"category": "rule-ignored",
				"cwd": "/workspace",
				"detail": "second event",
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
			INSERT INTO resolutions (category, pattern, resolved_at_utc, reference)
			VALUES (?, ?, ?, ?)
			""",
			("rule-ignored", "resolved", "2026-09-03T10:00:00+00:00", "fix"),
		)

	assert cli.main(["list", "--database", str(database_path), "--json"]) == 0

	assert [item["detail"] for item in json.loads(capsys.readouterr().out)["data"]] == [
		"resolved"
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


def test_list_shows_each_event_on_one_human_row(tmp_path, capsys) -> None:
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
				"2026-09-03T09:41:00+00:00",
				"wrong-approach",
				"/workspace",
				"tried a manual retry loop",
				"manual",
			),
		)

	exit_code = cli.main(["list", "--database", str(database_path)])
	captured = capsys.readouterr()

	assert exit_code == 0
	assert captured.err == ""
	assert "2026-09-03T09:41:00+00:00" in captured.out
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
			"schema_version": 2,
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
