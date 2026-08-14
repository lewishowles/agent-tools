import json
from pathlib import Path

from agents_progress import cli


def test_json_success_uses_the_stable_envelope(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"id": "prj_test", "slug": "agents", "name": "Agents"},
		"task": None,
		"chunk": None,
		"hint_command": "progress ready",
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db"), "--json"]) == 0

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_json_failure_has_no_prose_outside_the_error_envelope(
	tmp_path: Path, capsys
) -> None:
	assert (
		cli.main(
			[
				"task",
				"get",
				"chk_" + "c" * 22,
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 1
	)

	response = json.loads(capsys.readouterr().out)
	assert response["ok"] is False
	assert response["error"]["code"] == "wrong-id-type"


def test_human_success_renders_readable_output(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"id": "prj_test", "slug": "agents", "name": "Agents"},
		"task": {
			"id": "tsk_test",
			"title": "Read surface",
			"status": "in-progress",
			"overview": "Read the current task.",
		},
		"chunk": {
			"id": "chk_test",
			"title": "CLI output",
			"description": "Render readable output.",
			"status": "active",
		},
		"hint_command": "progress chunk complete chk_test",
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db")]) == 0

	output = capsys.readouterr()

	assert output.err == ""
	assert "Project: Agents" in output.out
	assert "Task: Read surface" in output.out
	assert "Next: progress chunk complete chk_test" in output.out
