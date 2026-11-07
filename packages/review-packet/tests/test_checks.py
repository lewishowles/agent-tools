"""Check draft verification with invented agent-run results and repositories."""

import json
import subprocess
from pathlib import Path

import pytest

import review_packet.checks as checks
import review_packet.cli as cli
from review_packet.checks import CheckState
from review_packet.progress_records import Chunk, RelatedTasks, Task
from review_packet.verification import render


def _git(root: Path, *arguments: str) -> None:
	"""Run one Git command in the invented repository.

	Args:
		root: The invented repository root.
		arguments: The Git arguments to run there.
	"""
	subprocess.run(["git", *arguments], cwd=root, capture_output=True, check=True)


@pytest.fixture
def draft_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	"""Create a small repository with one changed file and a check setting, with drafts written to a temporary folder outside it."""
	# The invented repository is separate from the draft destination.
	root = tmp_path / "repo"
	root.mkdir()
	_git(root, "init", "-q")
	_git(root, "config", "user.email", "review@example.invalid")
	_git(root, "config", "user.name", "Reviewer")
	(root / "README.md").write_text("Repository\n")
	(root / "changed.txt").write_text("before\n")
	_git(root, "add", ".")
	_git(root, "commit", "-qm", "initial")
	(root / "changed.txt").write_text("after\n")
	(root / ".review-packet.toml").write_text('checks = ["unit"]\n')
	# The progress record is invented; no shared database is opened.
	chunk = Chunk(
		"chk_example",
		"Review",
		"Review the change",
		"Is it correct?",
		Task("tsk_example", "Task", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "drafts"))
	monkeypatch.chdir(root)
	return root


def test_run_checks_keeps_each_state_and_first_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Report passed, failed, timed-out, missing and manual names in order."""
	# Each envelope follows the single-name agent-run JSON contract.
	responses = {
		"pass": {
			"ok": True,
			"data": {"argv": ["pytest", "tests"], "duration_seconds": 1.25},
		},
		"fail": {
			"ok": False,
			"error": {
				"code": "check-failed",
				"data": {
					"argv": ["ruff", "check"],
					"duration_seconds": 0.5,
					"failure": {
						"first": {
							"path": "tests/test_x.py",
							"line": 3,
							"title": "One failure",
							"detail": ["detail"],
						}
					},
				},
			},
		},
		"timeout": {
			"ok": False,
			"error": {
				"code": "check-failed",
				"data": {
					"argv": ["slow"],
					"timed_out": True,
					"duration_seconds": 3.0,
					"failure": {"tail": ["Earlier line", "Last line"]},
				},
			},
		},
		"missing": {
			"ok": False,
			"error": {"code": "not-found", "message": "Not registered"},
		},
		"manual": {
			"ok": False,
			"error": {"code": "manual", "data": {"argv": ["browser", "check"]}},
		},
	}
	# The agent-run commands the fake was asked to run.
	calls = []

	def fake_run(
		argv: list[str], **_kwargs: object
	) -> subprocess.CompletedProcess[str]:
		"""Return the invented envelope for a configured command.

		Args:
			argv: The agent-run invocation.
			_kwargs: Subprocess options from run_checks.

		Returns:
			A completed process holding one JSON envelope.
		"""
		calls.append(argv)
		return subprocess.CompletedProcess(argv, 0, json.dumps(responses[argv[2]]), "")

	monkeypatch.setattr(checks.subprocess, "run", fake_run)

	# One result per configured name, in the order the settings list them.
	results = checks.run_checks(tmp_path, tuple(responses))

	assert [item.state for item in results] == [
		CheckState.PASSED,
		CheckState.FAILED,
		CheckState.TIMED_OUT,
		CheckState.NOT_REGISTERED,
		CheckState.MANUAL,
	]
	assert results[1].failure == "tests/test_x.py:3\nOne failure\ndetail"
	assert results[2].failure == "Earlier line\nLast line"
	assert results[3].duration_seconds is None
	assert calls == [["agent-run", "run", name, "--json"] for name in responses]


def test_verification_makes_repository_paths_relative(tmp_path: Path) -> None:
	"""Keep the author's result clear without sharing the repository's path."""
	# A failure can contain the repository path in its command and output.
	result = checks.CheckResult(
		"unit",
		f"pytest {tmp_path}/tests {tmp_path}",
		CheckState.FAILED,
		0.125,
		f"{tmp_path}/tests/test_one.py: failed\n{tmp_path}-old/x\n/private{tmp_path}/x",
	)

	# The verification.md text the draft would write for this result.
	text = render(tmp_path, (result,))

	assert "reviewer has not re-run" in text
	assert "pytest tests ." in text
	assert "tests/test_one.py: failed" in text
	assert f"{tmp_path}-old/x" in text
	assert f"/private{tmp_path}/x" in text
	assert f"{tmp_path}/tests" not in text
	assert "0.125s" in text
	assert "No checks were configured." in render(tmp_path, ())


def test_draft_writes_verification_after_failed_check(
	draft_repo: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""A failed check still produces a draft and a scanned verification report."""
	# run_checks is replaced, so no project command runs.
	monkeypatch.setattr(
		cli,
		"run_checks",
		lambda _root, _names: (
			checks.CheckResult(
				"unit", "pytest tests", CheckState.FAILED, 0.2, "A failed test"
			),
		),
	)

	assert cli.main(["draft", "chk_example", "--json"]) == 0

	# The paths the draft command reports for the files it wrote.
	data = json.loads(capsys.readouterr().out)["data"]
	# The verification report written beside the brief.
	verification = Path(data["verification"])

	assert verification.exists()
	assert "A failed test" in verification.read_text()
	assert Path(data["brief"]).exists()
	assert Path(data["manifest"]).exists()


def test_draft_refuses_private_path_in_verification(
	draft_repo: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Refuse a check report containing a home path outside the repository."""
	# An invented home path in check output must not enter the packet.
	private_path = Path("/") / "Users" / "packet-reviewer" / "outside" / "result.txt"
	# run_checks is replaced, so no project command runs.
	monkeypatch.setattr(
		cli,
		"run_checks",
		lambda _root, _names: (
			checks.CheckResult(
				"unit", "pytest tests", CheckState.FAILED, 0.2, str(private_path)
			),
		),
	)

	assert cli.main(["draft", "chk_example", "--json"]) == 1

	# The refusal names the generated check report as the file holding the finding.
	error = json.loads(capsys.readouterr().out)["error"]
	# The brief would be the first draft file written.
	brief = cli._brief_path(draft_repo, "chk_example")

	assert error["code"] == "check-failed"
	assert any(item["path"] == "verification.md" for item in error["data"]["findings"])
	assert not brief.exists()
	assert not brief.with_name("manifest.json").exists()
	assert not brief.with_name("verification.md").exists()


def test_draft_refuses_file_changed_by_check(
	draft_repo: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Do not write any draft file against hashes changed by a check."""

	def fake_checks(
		root: Path, _names: tuple[str, ...]
	) -> tuple[checks.CheckResult, ...]:
		"""Change one selected file during an invented successful check.

		Args:
			root: The invented repository.
			_names: The configured check names.

		Returns:
			One successful check result.
		"""
		(root / "changed.txt").write_text("changed by check\n")
		return (
			checks.CheckResult("unit", "pytest tests", CheckState.PASSED, 0.1, None),
		)

	monkeypatch.setattr(cli, "run_checks", fake_checks)

	assert cli.main(["draft", "chk_example", "--json"]) == 1

	# The refusal names the file the check changed.
	error = json.loads(capsys.readouterr().out)["error"]
	# The brief would be the first draft file written.
	brief = cli._brief_path(draft_repo, "chk_example")

	assert error["code"] == "check-failed"
	assert error["data"]["paths"] == ["changed.txt"]
	assert not brief.exists()
	assert not brief.with_name("manifest.json").exists()
	assert not brief.with_name("verification.md").exists()
