#!/usr/bin/env bash
# Prove the driver ledger against synthetic Case 1 and Case 2 Claude and Codex sessions.
set -euo pipefail

# The directory containing this ledger test script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The repository root used to run the usage report.
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# The fixture directory for the first pair of sessions.
FIXTURE_DIR="$SCRIPT_DIR/fixtures/usage-case-1"

# The report command output for the first fixture.
output=$(
	cd "$REPO_DIR"
	# The Claude and Codex session directories used by the report command.
	CLAUDE_CONFIG_DIR="$FIXTURE_DIR/claude" \
	CODEX_HOME="$FIXTURE_DIR/codex" \
	python3 scripts/audit/token_usage_report.py --since 2026-08-01 --until 2026-08-01 2>&1
)
printf '%s\n' "$output" | tail -20

cd "$REPO_DIR"
python3 - <<'PY'
import json
from pathlib import Path


# The folder containing the generated usage reports.
report_directory = Path(".agent/audits/usage")
# The generated usage report for the first fixture.
report = json.loads((report_directory / "latest.json").read_text(encoding="utf-8"))
# The generated Markdown summary report.
markdown = (report_directory / "latest.md").read_text(encoding="utf-8")
# The detailed generated Markdown report.
detail_markdown = (report_directory / "latest-detail.md").read_text(encoding="utf-8")

assert report["session_count"] == 2
assert report["driver_reconciliation"]["reconciles"] is True
assert report["driver_reconciliation"]["tool_call_count"] == 16
assert report["driver_reconciliation"]["attributed_count"] == 16
assert report["driver_reconciliation"]["unattributed_count"] == 0

# The reported sessions indexed by tool.
sessions = {session["tool"]: session for session in report["sessions"]}
# The Claude session whose totals and drivers are checked.
claude = sessions["Claude"]
# The Codex session whose totals and drivers are checked.
codex = sessions["Codex"]

assert claude["tokens"] == {
	"cache_creation_input_tokens": 30,
	"cache_read_input_tokens": 45,
	"input_tokens": 150,
	"output_tokens": 65,
	"total_input_tokens": 225,
	"total_tokens": 290,
}
assert codex["tokens"] == {
	"cached_input_tokens": 30,
	"input_tokens": 120,
	"output_tokens": 45,
	"reasoning_output_tokens": 15,
	"total_tokens": 210,
}

for session in (claude, codex):
	assert session["tool_call_count"] == sum(row["count"] for row in session["driver_ledger"])
	assert session["tool_call_count"] == (9 if session["tool"] == "Claude" else 7)
	assert session["driver_reconciles"] is True
	assert all(row["method"] == "chars/4" for row in session["driver_ledger"])
	assert session["driver_ledger"] == sorted(
		session["driver_ledger"],
		key=lambda row: (-row["payload_estimate_tokens"], row["category"], row["key"]),
	)

# The Claude driver rows indexed by category and key.
claude_rows = {(row["category"], row["key"]): row for row in claude["driver_ledger"]}
# The Codex driver rows indexed by category and key.
codex_rows = {(row["category"], row["key"]): row for row in codex["driver_ledger"]}
assert claude_rows[("bash", "rg")]["count"] == 1
assert claude_rows[("read", "/fixture/claude/project/src/app.py")]["repeated"] is True
assert claude_rows[("edit", "/fixture/claude/project/src/app.py")]["repeated"] is True
assert claude_rows[("write", "/fixture/claude/project/src/config.py")]["count"] == 1
assert claude_rows[("skill", "code-style")]["count"] == 1
assert claude_rows[("hook", "progress-resume")]["count"] == 1
assert claude_rows[("tool", "Task")]["count"] == 1
assert codex_rows[("bash", "git")]["count"] == 1
assert codex_rows[("bash", "cat")]["count"] == 2
assert codex_rows[("edit", "/fixture/codex/project/src/app.py")]["count"] == 1
assert codex_rows[("tool", "spawn_agent")]["count"] == 1

for forbidden in (
	"SYNTHETIC_CLAUDE_HOOK_STDOUT_8F3C",
	"SYNTHETIC_CLAUDE_READ_RESULT_1A7E",
	"SYNTHETIC_CLAUDE_DELEGATION_RESULT_4C2B",
	"SYNTHETIC_CODEX_COMMAND_RESULT_6D9A",
	"SYNTHETIC_CODEX_DELEGATION_RESULT_3B1F",
	"SYNTHETIC_CODEX_MCP_RESULT_5E8D",
):
	assert forbidden not in markdown
	assert forbidden not in detail_markdown
	assert forbidden not in json.dumps(report)

assert "| Session | Tool calls | Total tokens |" in markdown
assert "/fixture/claude/project/src/app.py" in detail_markdown
assert "## Driver ledger (ranked aggregate)" in detail_markdown
assert "## Driver ledger by session" in detail_markdown
print("usage driver ledger Case 1: PASS")
PY

printf '%s\n' 'Running usage driver ledger Case 2'
# The fixture directory for the retry and failure case.
CASE2_FIXTURE_DIR="$SCRIPT_DIR/fixtures/usage-case-2"
output=$(
	cd "$REPO_DIR"
	CLAUDE_CONFIG_DIR="$CASE2_FIXTURE_DIR/claude" \
	CODEX_HOME="$CASE2_FIXTURE_DIR/codex" \
	python3 scripts/audit/token_usage_report.py --since 2026-08-01 --until 2026-08-01 2>&1
)
printf '%s\n' "$output" | tail -20

cd "$REPO_DIR"
python3 - <<'PY'
import json
from pathlib import Path


# The folder containing the generated usage reports.
report_directory = Path(".agent/audits/usage")
# The generated usage report for the retry and failure case.
report = json.loads((report_directory / "latest.json").read_text(encoding="utf-8"))

assert report["session_count"] == 2
assert report["driver_reconciliation"]["tool_call_count"] == 5
assert report["driver_reconciliation"]["attributed_count"] == 5
assert report["driver_reconciliation"]["unattributed_count"] == 0
assert report["driver_reconciliation"]["reconciles"] is True

# The reported sessions indexed by tool.
sessions = {session["tool"]: session for session in report["sessions"]}
# The Claude session with retried calls.
claude = sessions["Claude"]
# The Codex session with a successful call.
codex = sessions["Codex"]
# The Claude driver rows indexed by category and key.
claude_rows = {(row["category"], row["key"]): row for row in claude["driver_ledger"]}
# The Codex driver rows indexed by category and key.
codex_rows = {(row["category"], row["key"]): row for row in codex["driver_ledger"]}

assert claude["tool_call_count"] == 4
assert claude["driver_reconciles"] is True
assert claude["tokens"]["total_tokens"] == 42
assert "payload_estimate_tokens" not in claude["tokens"]
assert claude["driver_ledger"] == sorted(
	claude["driver_ledger"],
	key=lambda row: (-row["payload_estimate_tokens"], row["category"], row["key"]),
)
assert claude_rows[("bash", "rg")]["count"] == 2
assert claude_rows[("bash", "rg")]["failure_count"] == 1
assert claude_rows[("bash", "rg")]["retry_count"] == 1
assert claude_rows[("read", "/fixture/claude/project/src/app.py")]["count"] == 2
assert claude_rows[("read", "/fixture/claude/project/src/app.py")]["failure_count"] == 1
assert claude_rows[("read", "/fixture/claude/project/src/app.py")]["retry_count"] == 1

assert codex["tool_call_count"] == 1
assert codex["driver_reconciles"] is True
assert codex_rows[("bash", "rg")]["failure_count"] == 0
assert codex_rows[("bash", "rg")]["retry_count"] == 0

# The combined driver rows indexed by category and key.
aggregate_rows = {(row["category"], row["key"]): row for row in report["driver_ledger"]}
assert aggregate_rows[("bash", "rg")]["count"] == 3
assert aggregate_rows[("bash", "rg")]["failure_count"] == 1
assert aggregate_rows[("bash", "rg")]["retry_count"] == 1
assert aggregate_rows[("read", "/fixture/claude/project/src/app.py")]["count"] == 2
assert aggregate_rows[("read", "/fixture/claude/project/src/app.py")]["failure_count"] == 1
assert aggregate_rows[("read", "/fixture/claude/project/src/app.py")]["retry_count"] == 1

for session in (claude, codex):
	assert session["tool_call_count"] == sum(row["count"] for row in session["driver_ledger"])
	assert all(row["method"] == "chars/4" for row in session["driver_ledger"])
	assert all(row["payload_estimate_tokens"] > 0 for row in session["driver_ledger"])

assert report["driver_ledger"] == sorted(
	report["driver_ledger"],
	key=lambda row: (-row["payload_estimate_tokens"], row["category"], row["key"]),
)
assert report["driver_ledger"]
assert all("payload_estimate_tokens" in row for row in report["driver_ledger"])
print("usage driver ledger Case 2: PASS")
PY

printf '%s\n' 'Running usage partial-data Case 3'
# The fixture directory for the partial data case.
CASE3_FIXTURE_DIR="$SCRIPT_DIR/fixtures/usage-case-3"
output=$(
	cd "$REPO_DIR"
	CLAUDE_CONFIG_DIR="$CASE3_FIXTURE_DIR/claude" \
	CODEX_HOME="$CASE3_FIXTURE_DIR/codex" \
	python3 scripts/audit/token_usage_report.py --since 2026-08-01 --until 2026-08-01 2>&1
)
printf '%s\n' "$output" | tail -20

cd "$REPO_DIR"
python3 - <<'PY'
import json
from pathlib import Path


# The folder containing the generated usage reports.
report_directory = Path(".agent/audits/usage")
# The generated usage report for the partial data case.
report = json.loads((report_directory / "latest.json").read_text(encoding="utf-8"))
# The generated Markdown summary report.
markdown = (report_directory / "latest.md").read_text(encoding="utf-8")
# The detailed generated Markdown report.
detail_markdown = (report_directory / "latest-detail.md").read_text(encoding="utf-8")

assert report["session_count"] == 4
assert report["partial_data"] == {
	"partial": True,
	"skipped_record_count": 8,
	"skipped_record_counts": {"Claude": 4, "Codex": 4},
}

# The reported sessions indexed by tool and session ID.
sessions = {(session["tool"], session["session_id"]): session for session in report["sessions"]}
assert sessions[("Claude", "claude-case-3")]["tokens"]["total_tokens"] == 28
assert sessions[("Claude", "claude-case-3")]["skipped_record_count"] == 2
assert sessions[("Codex", "codex-case-3")]["tokens"]["total_tokens"] == 38
assert sessions[("Codex", "codex-case-3")]["skipped_record_count"] == 2
assert sessions[("Claude", "claude-case-3-skipped-only")]["tokens"]["total_tokens"] == 0
assert sessions[("Claude", "claude-case-3-skipped-only")]["skipped_record_count"] == 2
assert sessions[("Codex", "codex-case-3-skipped-only")]["tokens"]["total_tokens"] == 0
assert sessions[("Codex", "codex-case-3-skipped-only")]["skipped_record_count"] == 2
assert "## Partial data" in detail_markdown
assert "Skipped records: **8**" in detail_markdown
assert "| Claude | 4 |" in detail_markdown
assert "| Codex | 4 |" in detail_markdown
print("usage partial-data Case 3: PASS")
PY

printf '%s\n' 'Running usage correctness regressions'
cd "$REPO_DIR"
python3 - <<'PY'
from copy import deepcopy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.audit import token_usage_report
from scripts.audit.token_usage_rendering import (
	render_detail_markdown,
	render_markdown,
)


with tempfile.TemporaryDirectory() as temporary_directory:
	# The absent transcript path used to test a failed read.
	missing_path = Path(temporary_directory) / "missing.jsonl"
	try:
		list(token_usage_report.records(missing_path, token_usage_report.CLAUDE_RECORD_TYPES, {"skipped_record_count": 0}))
	except FileNotFoundError:
		pass
	else:
		raise AssertionError("missing transcript reads must raise FileNotFoundError")


# The usage record containing finite and non-finite token counts.
parsed_usage = json.loads(
	'{"input_tokens": 3.9, "cached_input_tokens": NaN, '
	'"reasoning_output_tokens": Infinity, "output_tokens": -Infinity, '
	'"total_tokens": Infinity}'
)
assert token_usage_report.usage_totals(parsed_usage, "Codex") == {
	"input_tokens": 3,
	"cached_input_tokens": 0,
	"reasoning_output_tokens": 0,
	"output_tokens": 0,
	"total_tokens": 3,
}
assert token_usage_report.number(float("nan")) == 0
assert token_usage_report.number(float("inf")) == 0
assert token_usage_report.number(float("-inf")) == 0


class FakeQuery:
	def fetchall(self):
		return []


class FakeConnection:
	def __init__(self, query_error=False):
		# Whether the fake database query should fail.
		self.query_error = query_error
		# Whether the fake database connection has been closed.
		self.closed = False

	def execute(self, query):
		if self.query_error:
			raise token_usage_report.sqlite3.OperationalError("synthetic query failure")

		return FakeQuery()

	def close(self):
		self.closed = True


with tempfile.TemporaryDirectory() as temporary_directory:
	# The temporary database file used to load HCOM labels.
	database_path = Path(temporary_directory) / "hcom.db"
	database_path.touch()
	# The real HCOM database path restored after the test.
	previous_database = token_usage_report.HCOM_DATABASE
	token_usage_report.HCOM_DATABASE = database_path
	try:
		# The fake connection used for a successful label lookup.
		success_connection = FakeConnection()
		with patch.object(token_usage_report.sqlite3, "connect", return_value=success_connection):
			assert token_usage_report.load_hcom_labels() == ({}, {})
		assert success_connection.closed

		# The fake connection used for a failed label lookup.
		error_connection = FakeConnection(query_error=True)
		with patch.object(token_usage_report.sqlite3, "connect", return_value=error_connection):
			assert token_usage_report.load_hcom_labels() == ({}, {})
		assert error_connection.closed
	finally:
		token_usage_report.HCOM_DATABASE = previous_database

# The folder containing the generated usage reports.
report_directory = Path(".agent/audits/usage")
# The generated usage report used for rendering checks.
report = json.loads((report_directory / "latest.json").read_text(encoding="utf-8"))
# A copy of the report holding only a session with tokens but no driver calls.
no_driver_report = deepcopy(report)
# A copy of a session given tokens but no driver calls.
no_driver_session = deepcopy(report["sessions"][0])
no_driver_session["session_id"] = "codex-no-driver"
no_driver_session["tool"] = "Codex"
no_driver_session["tokens"] = {"total_tokens": 123}
no_driver_session["tool_call_count"] = 0
no_driver_session["unattributed_count"] = 0
no_driver_session["unattributed"] = {
	"count": 0,
	"payload_estimate_tokens": 0,
	"method": "chars/4",
}
no_driver_session["driver_ledger"] = []
no_driver_report["sessions"] = [no_driver_session]

# The summary rendered for the session without drivers.
summary = render_markdown(no_driver_report)
# The detail rendered for the session without drivers.
detail = render_detail_markdown(no_driver_report)
# The detail line expected for tokens without a driver.
expected_no_driver_line = (
	"Codex `codex-no-driver`: 123 tokens, no attributed tool-call driver."
)
assert expected_no_driver_line not in summary
assert expected_no_driver_line in detail
assert "### Codex `codex-no-driver`" not in detail

# A copy of the report that adds a session with no activity.
zero_activity_report = deepcopy(no_driver_report)
# A copy of a session with no tokens or driver calls.
zero_activity_session = deepcopy(report["sessions"][0])
zero_activity_session["session_id"] = "codex-zero-activity"
zero_activity_session["tool"] = "Codex"
zero_activity_session["tokens"] = {"total_tokens": 0}
zero_activity_session["tool_call_count"] = 0
zero_activity_session["unattributed_count"] = 0
zero_activity_session["unattributed"] = {
	"count": 0,
	"payload_estimate_tokens": 0,
	"method": "chars/4",
}
zero_activity_session["driver_ledger"] = []
zero_activity_report["sessions"] = [no_driver_session, zero_activity_session]
zero_activity_report["top_sessions"] = []

# The summary rendered with a zero activity session.
zero_activity_summary = render_markdown(zero_activity_report)
# The detail rendered with a zero activity session.
zero_activity_detail = render_detail_markdown(zero_activity_report)
# The explanation expected when an inactive session is omitted.
expected_omitted_line = (
	"1 session had no attributable activity in the selected window and is omitted."
)
assert "| Codex `codex-zero-activity` |" not in zero_activity_summary
assert "### Codex `codex-zero-activity`" not in zero_activity_detail
assert expected_omitted_line in zero_activity_summary
assert expected_omitted_line in zero_activity_detail

# A copy of the report used to check the order of visible sessions.
ordered_report = deepcopy(zero_activity_report)
# The session with activity that should remain visible.
ordered_visible_session = deepcopy(report["sessions"][0])
ordered_visible_session["session_id"] = "claude-visible"
ordered_visible_session["tool"] = "Claude"
ordered_visible_session["tokens"] = {"total_tokens": 456}
ordered_visible_session["tool_call_count"] = 1
ordered_visible_session["unattributed_count"] = 0
ordered_visible_session["unattributed"] = {
	"count": 0,
	"payload_estimate_tokens": 0,
	"method": "chars/4",
}
ordered_visible_session["driver_ledger"] = []
ordered_report["sessions"] = [
	no_driver_session,
	ordered_visible_session,
	zero_activity_session,
]

# The summary rendered for the session ordering check.
ordered_summary = render_markdown(ordered_report)
# The detail rendered for the session ordering check.
ordered_detail = render_detail_markdown(ordered_report)
assert ordered_summary.index("| Codex `codex-no-driver` |") < ordered_summary.index(
	"| Claude `claude-visible` |"
)
assert ordered_detail.index("Codex `codex-no-driver`:") < ordered_detail.index(
	"### Claude `claude-visible`"
)

print("usage correctness regressions: PASS")
PY
