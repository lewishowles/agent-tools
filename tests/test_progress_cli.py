import dataclasses
import json
from pathlib import Path

import pytest
from agents_progress.database import Database
import agents_progress.render as render_module
import agents_progress.style as style_module
from agents_progress import cli
from agents_progress.errors import (
	AlreadyExistsError,
	DuplicateDependencyError,
	ProgressError,
)
from agents_progress.models import Task
from agents_progress.projects import Project, ProjectStore
from agents_progress.render import _status_result_type
from agents_progress.writes import WriteStore


def _stderr_error_message(captured_err: str) -> str:
	"""Return the bare error message from captured stderr, with the status marker, "Error" label, and ANSI codes removed."""
	stripped = render_module._ANSI_ESCAPE_PATTERN.sub("", captured_err)
	_, _, after_marker = stripped.partition(" ")

	return after_marker.removeprefix("Error ").rstrip("\n")


def test_bare_invocation_prints_help_and_succeeds(capsys) -> None:
	assert cli.main([]) == 0

	output = capsys.readouterr()

	assert output.err == ""
	assert output.out == cli.build_parser().format_help()


@pytest.mark.parametrize(
	("command", "expected_choices"),
	[
		("project", "{init,attach,current}"),
		("release", "{add,list,get,remove,rename,edit,complete}"),
		(
			"task",
			"{add,move,dependency,remove,clean,rename,edit,start,complete,block,unblock,get,list}",
		),
		("chunk", "{add,move,start,complete,remove,rename,edit,get,list}"),
		("discovery", "{add,list,remove}"),
		("decision", "{add,list,remove}"),
		("context", "{get,set}"),
	],
)
def test_missing_noun_subcommand_lists_valid_choices(
	capsys, command: str, expected_choices: str
) -> None:
	assert cli.main([command]) == 2

	output = capsys.readouterr()

	assert output.out == ""
	assert _stderr_error_message(output.err) == (
		f"the following arguments are required: {expected_choices}"
	)


@pytest.mark.parametrize(
	("arguments", "token", "expected_suggestions", "expected_message"),
	[
		(
			["list"],
			"list",
			[
				"progress release list",
				"progress task list",
				"progress chunk list",
				"progress discovery list",
				"progress decision list",
			],
			None,
		),
		(
			["add"],
			"add",
			[
				"progress release add",
				"progress task add",
				"progress task dependency add",
				"progress chunk add",
				"progress discovery add",
				"progress decision add",
			],
			None,
		),
		(
			["get"],
			"get",
			[
				"progress release get",
				"progress task get",
				"progress chunk get",
				"progress context get",
			],
			None,
		),
		(
			["current"],
			"current",
			[],
			"'current' was removed. Use progress next instead.",
		),
		(["ready"], "ready", [], "'ready' was removed. Use progress next instead."),
		(
			["task", "foo"],
			"foo",
			[
				"progress task add",
				"progress task move",
				"progress task dependency add",
				"progress task dependency remove",
				"progress task remove",
				"progress task clean",
				"progress task rename",
				"progress task edit",
				"progress task start",
				"progress task complete",
				"progress task block",
				"progress task unblock",
				"progress task get",
				"progress task list",
			],
			None,
		),
		(["taks", "list"], "taks", ["progress task list"], None),
		(["zzz"], "zzz", [], None),
	],
)
def test_unknown_commands_explain_the_correct_command_shape(
	capsys,
	arguments: list[str],
	token: str,
	expected_suggestions: list[str],
	expected_message: str | None,
) -> None:
	assert cli.main(arguments) == 2

	output = capsys.readouterr()

	assert output.out == ""
	if expected_message is not None:
		assert _stderr_error_message(output.err) == expected_message
	elif expected_suggestions:
		expected_lines = "\n".join(
			f"  {suggestion}" for suggestion in expected_suggestions
		)
		expected_error = (
			f"'{token}' is not a command on its own. Did you mean one of:\n"
			f"{expected_lines}"
		)
		assert _stderr_error_message(output.err) == expected_error
	else:
		assert f"invalid choice: '{token}'" in output.err
		assert "(choose from" in output.err
		assert "Did you mean one of:" not in output.err


@pytest.mark.parametrize("command", ["current", "ready"])
def test_legacy_command_json_uses_the_same_replacement(capsys, command: str) -> None:
	assert cli.main([command]) == 2

	human_output = capsys.readouterr()

	assert cli.main([command, "--json"]) == 2

	json_output = capsys.readouterr()
	response = json.loads(json_output.out)

	assert json_output.err == ""
	assert response == {
		"ok": False,
		"error": {
			"code": "usage",
			"message": _stderr_error_message(human_output.err),
			"details": {},
		},
	}
	assert response["error"]["message"] == (
		f"'{command}' was removed. Use progress next instead."
	)


def test_unknown_command_json_uses_the_same_multiline_suggestion(capsys) -> None:
	assert cli.main(["list"]) == 2

	human_output = capsys.readouterr()

	assert cli.main(["list", "--json"]) == 2

	json_output = capsys.readouterr()
	response = json.loads(json_output.out)

	assert json_output.err == ""
	assert response == {
		"ok": False,
		"error": {
			"code": "usage",
			"message": _stderr_error_message(human_output.err),
			"details": {},
		},
	}
	assert (
		"Did you mean one of:\n  progress release list" in response["error"]["message"]
	)


def test_top_level_help_lists_commands_without_a_redundant_metavar() -> None:
	help_text = cli.build_parser().format_help()
	usage_line = help_text.splitlines()[0]

	assert "COMMAND" in usage_line
	assert "{next,current,doctor" not in usage_line
	assert "\ncommands:\n" in help_text
	assert "\n  COMMAND\n" not in help_text
	assert "  task           read task records" in help_text
	assert "    task           read task records" not in help_text


def test_nested_help_lists_commands_without_a_redundant_metavar(capsys) -> None:
	with pytest.raises(SystemExit) as exception:
		cli.build_parser().parse_args(["project", "--help"])

	help_text = capsys.readouterr().out

	assert exception.value.code == 0
	assert "\ncommands:\n" in help_text
	assert "\n  {init,attach,current}\n" not in help_text
	assert "\n  init" in help_text
	assert "\n    init" not in help_text
	assert "create and bind a project" in help_text


def test_commands_json_lists_the_registry_with_required_flags(
	capsys,
) -> None:
	assert cli.main(["commands", "--json"]) == 0

	response = json.loads(capsys.readouterr().out)
	manifest = response["data"]
	commands = {item["path"]: item for item in manifest}

	assert response["ok"] is True
	assert commands["task"]["help"] == "read task records"
	assert commands["task"]["flags"] == []
	assert commands["task dependency add"]["flags"] == [
		{"names": ["task_id"], "required": True},
		{"names": ["depends_on_task_id"], "required": True},
		{"names": ["--json"], "required": False},
		{"names": ["--database"], "required": False},
	]
	assert commands["task clean"]["flags"] == [
		{"names": ["--force"], "required": False},
		{"names": ["--json"], "required": False},
		{"names": ["--database"], "required": False},
	]
	assert commands["discovery add"]["flags"][1] == {
		"names": ["body"],
		"required": True,
	}
	assert commands["task add"]["flags"][9] == {
		"names": ["--release", "--release-id"],
		"required": False,
	}
	assert commands["release list"]["flags"][-4:] == [
		{"names": ["--limit"], "required": False},
		{"names": ["--offset"], "required": False},
		{"names": ["--json"], "required": False},
		{"names": ["--database"], "required": False},
	]


def test_commands_human_output_lists_paths_and_flag_requirements(capsys) -> None:
	assert cli.main(["commands"]) == 0

	output = capsys.readouterr().out

	assert "Command" in output
	assert "Description" in output
	assert "task dependency add" in output
	assert "task_id (required)" in output
	assert "--json (optional)" in output


def test_json_success_uses_the_stable_envelope(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"id": "prj_test", "slug": "agents", "name": "Agents"},
		"task": None,
		"chunk": None,
		"hint_command": "progress next",
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db"), "--json"]) == 0

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	("data", "expected_output"),
	[
		({"findings": [], "ok": True}, "Doctor: clean"),
		(
			{
				"findings": [
					{
						"field": "release.overview",
						"id": "rel_test",
						"noun": "release",
						"title": "Blank release",
					}
				],
				"ok": False,
			},
			"- release.overview: Blank release (rel_test)",
		),
	],
)
def test_doctor_human_output_reports_findings_or_clean(
	tmp_path: Path, monkeypatch, capsys, data, expected_output: str
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def doctor(self):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["doctor", "--database", str(tmp_path / "db")]) == 0

	assert expected_output in capsys.readouterr().out


def test_doctor_dispatches_with_the_json_envelope(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"findings": [], "ok": True}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def doctor(self):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["doctor", "--database", str(tmp_path / "db"), "--json"]) == 0

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	("arguments", "method_name"),
	[
		(["context", "get"], "context_get"),
		(["discovery", "list"], "discovery_list"),
		(
			["decision", "list", "--task", "tsk_" + "t" * 22],
			"decision_list",
		),
		(["release", "get", "rel_" + "r" * 22], "release_get"),
		(["chunk", "get", "chk_" + "c" * 22], "chunk_get"),
	],
)
def test_new_read_commands_dispatch_with_the_json_envelope(
	tmp_path: Path,
	monkeypatch,
	capsys,
	arguments: list[str],
	method_name: str,
) -> None:
	data = {"id": "obj_test"}
	calls: list[str] = []

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def handler(*arguments, **keyword_arguments):
				calls.append(name)
				return data

			return handler

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				*arguments,
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert calls == [method_name]
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	("arguments", "method_name", "store_name"),
	[
		(
			["project", "attach", "prj_" + "p" * 22],
			"attach",
			"project",
		),
		(
			[
				"release",
				"add",
				"--slug",
				"release",
				"--title",
				"Release",
				"--overview",
				"Release overview",
			],
			"release_add",
			"write",
		),
		(
			[
				"task",
				"add",
				"--slug",
				"task",
				"--title",
				"Task",
				"--overview",
				"Task overview",
				"--purpose",
				"Task purpose",
				"--contract",
				"Task contract",
			],
			"task_add",
			"write",
		),
		(
			[
				"task",
				"move",
				"tsk_" + "t" * 22,
				"--release",
				"rel_" + "r" * 22,
				"--before",
				"tsk_" + "b" * 22,
			],
			"task_move",
			"write",
		),
		(
			["task", "dependency", "add", "tsk_" + "t" * 22, "tsk_" + "d" * 22],
			"task_dependency_add",
			"write",
		),
		(
			[
				"task",
				"dependency",
				"remove",
				"tsk_" + "t" * 22,
				"tsk_" + "d" * 22,
			],
			"task_dependency_remove",
			"write",
		),
		(["release", "remove", "rel_" + "r" * 22], "release_remove", "write"),
		(
			["release", "rename", "rel_" + "r" * 22, "--title", "Renamed release"],
			"release_rename",
			"write",
		),
		(
			["release", "edit", "rel_" + "r" * 22, "--overview", "Updated overview"],
			"release_edit",
			"write",
		),
		(["release", "complete", "rel_" + "r" * 22], "release_complete", "write"),
		(["task", "remove", "tsk_" + "t" * 22], "task_remove", "write"),
		(["task", "clean"], "task_clean", "write"),
		(
			["task", "rename", "tsk_" + "t" * 22, "--title", "Renamed task"],
			"task_rename",
			"write",
		),
		(
			["task", "edit", "tsk_" + "t" * 22, "--overview", "Updated"],
			"task_edit",
			"write",
		),
		(["task", "start", "tsk_" + "t" * 22], "task_start", "write"),
		(["task", "complete", "tsk_" + "t" * 22], "task_complete", "write"),
		(
			["task", "block", "tsk_" + "t" * 22, "--reason", "Waiting"],
			"task_block",
			"write",
		),
		(["task", "unblock", "tsk_" + "t" * 22], "task_unblock", "write"),
		(
			[
				"chunk",
				"add",
				"--task",
				"tsk_" + "t" * 22,
				"--title",
				"Chunk",
				"--description",
				"Chunk description",
			],
			"chunk_add",
			"write",
		),
		(
			[
				"chunk",
				"move",
				"chk_" + "c" * 22,
				"--before",
				"chk_" + "b" * 22,
			],
			"chunk_move",
			"write",
		),
		(["chunk", "complete", "chk_" + "c" * 22], "chunk_complete", "write"),
		(["chunk", "start", "chk_" + "c" * 22], "chunk_start", "write"),
		(["chunk", "remove", "chk_" + "c" * 22], "chunk_remove", "write"),
		(
			["chunk", "rename", "chk_" + "c" * 22, "--title", "Renamed chunk"],
			"chunk_rename",
			"write",
		),
		(
			["chunk", "edit", "chk_" + "c" * 22, "--description", "Updated"],
			"chunk_edit",
			"write",
		),
		(
			["discovery", "add", "--task", "tsk_" + "t" * 22, "A", "discovery"],
			"discovery_add",
			"write",
		),
		(
			["discovery", "remove", "nte_" + "n" * 22],
			"discovery_remove",
			"write",
		),
		(
			["decision", "add", "--task", "tsk_" + "t" * 22, "A", "decision"],
			"decision_add",
			"write",
		),
		(
			["decision", "remove", "nte_" + "n" * 22],
			"decision_remove",
			"write",
		),
		(["context", "set", "--current-goal", "Goal"], "context_set", "write"),
	],
)
def test_write_commands_dispatch_to_the_matching_store_method(
	tmp_path: Path,
	monkeypatch,
	capsys,
	arguments: list[str],
	method_name: str,
	store_name: str,
) -> None:
	data = {"id": "obj_test"}
	calls: list[tuple[str, str]] = []

	class _Project:
		def to_dict(self):
			return data

	class _ProjectStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def handler(*arguments, **keyword_arguments):
				calls.append(("project", name))
				return _Project()

			return handler

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def handler(*arguments, **keyword_arguments):
				calls.append(("write", name))
				return data

			return handler

	monkeypatch.setattr(cli, "ProjectStore", _ProjectStore)
	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert cli.main([*arguments, "--database", str(tmp_path / "db"), "--json"]) == 0

	assert calls == [(store_name, method_name)]
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_task_add_prompts_for_required_and_optional_arguments(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "tsk_test"}
	arguments_seen: dict[str, object] = {}
	prompt_values = iter(
		[
			"task-slug",
			"Task title",
			"Task overview",
			"Task purpose",
			"Task contract",
			"",
			"",
			"",
			"",
			"",
			"",
			"",
		]
	)
	prompts: list[str] = []

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_add(self, **arguments):
			arguments_seen.update(arguments)
			return data

	def fake_input(prompt: str) -> str:
		prompts.append(prompt)
		return next(prompt_values)

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	monkeypatch.setattr(cli, "render", lambda command, data: "")
	monkeypatch.setattr(cli, "prompt", fake_input)
	monkeypatch.setattr(
		cli.sys,
		"stdin",
		type("_InteractiveStdin", (), {"isatty": lambda self: True})(),
	)

	assert cli.main(["task", "add", "--database", str(tmp_path / "db")]) == 0

	assert arguments_seen == {
		"slug": "task-slug",
		"title": "Task title",
		"overview": "Task overview",
		"purpose": "Task purpose",
		"contract": "Task contract",
		"files": None,
		"acceptance_criteria": "",
		"verification": "",
		"risks": "",
		"release_id": None,
		"depends_on": [],
		"position": None,
	}
	assert prompts == [
		"slug: ",
		"title: ",
		"overview: ",
		"purpose: ",
		"contract: ",
		"files: ",
		"acceptance-criteria: ",
		"verification: ",
		"risks: ",
		"release: ",
		"depends-on: ",
		"position: ",
	]

	output = capsys.readouterr()
	assert output.err == ""
	assert all(
		f") {hint}" in output.out
		for hint in (
			"Short identifier stored on the task",
			"Display title",
			"Non-empty task summary",
			"Non-empty task purpose",
			"Non-empty task contract",
			"Optional files covered by the task; press Enter to skip",
			"Optional completion conditions; press Enter to skip",
			"Optional verification instructions; press Enter to skip",
			"Optional risks; press Enter to skip",
			"Associate the task with a release; press Enter to skip",
			"Task ID dependency; press Enter to skip",
			"Optional ordering position; press Enter to skip",
		)
	)
	assert all(
		flag in output.out
		for flag in (
			"--slug",
			"--title",
			"--overview",
			"--purpose",
			"--contract",
			"--files",
			"--acceptance-criteria",
			"--verification",
			"--risks",
			"--release/--release-id",
			"--depends-on/--dependency",
			"--position",
		)
	)
	assert output.out.count("press Enter to skip") == 7
	assert output.out.startswith("\n")
	assert output.out.count("\n\n") == len(prompts) - 1


def test_chunk_add_prompts_for_required_and_optional_arguments(
	tmp_path: Path, monkeypatch
) -> None:
	data = {"id": "chk_test", "task_id": "tsk_test"}
	arguments_seen: dict[str, object] = {}
	prompt_values = iter(["tsk_test", "Chunk title", "Chunk description", ""])

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def chunk_add(self, **arguments):
			arguments_seen.update(arguments)
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	monkeypatch.setattr(cli, "render", lambda command, data: "")
	monkeypatch.setattr(cli, "prompt", lambda prompt: next(prompt_values))
	monkeypatch.setattr(
		cli.sys,
		"stdin",
		type("_InteractiveStdin", (), {"isatty": lambda self: True})(),
	)

	assert cli.main(["chunk", "add", "--database", str(tmp_path / "db")]) == 0

	assert arguments_seen == {
		"task_id": "tsk_test",
		"title": "Chunk title",
		"description": "Chunk description",
		"position": None,
	}


def test_prompt_value_uses_editable_prompt(monkeypatch) -> None:
	argument = cli._PromptArgument(("--slug",), "short identifier", required=True)
	prompts: list[str] = []

	def fake_prompt(prompt: str) -> str:
		prompts.append(prompt)
		return "task-slug"

	monkeypatch.setattr(cli, "prompt", fake_prompt)

	assert cli._prompt_value(argument) == "task-slug"

	assert prompts == ["slug: "]


def test_prompt_value_treats_eof_as_a_blank_answer(monkeypatch) -> None:
	argument = cli._PromptArgument(("--slug",), "short identifier", required=True)

	def raise_eof(prompt: str) -> str:
		raise EOFError

	monkeypatch.setattr(cli, "prompt", raise_eof)

	assert cli._prompt_value(argument) == ""


def test_prompt_value_exits_130_when_cancelled(capsys, monkeypatch) -> None:
	argument = cli._PromptArgument(("--slug",), "short identifier", required=True)

	def raise_keyboard_interrupt(prompt: str) -> str:
		raise KeyboardInterrupt

	monkeypatch.setattr(cli, "prompt", raise_keyboard_interrupt)

	with pytest.raises(SystemExit) as error:
		cli._prompt_value(argument)

	assert error.value.code == 130
	assert capsys.readouterr().out.endswith("Cancelled.\n")


@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_add_help_does_not_prompt_for_missing_required_arguments(
	help_flag: str, capsys, monkeypatch
) -> None:
	monkeypatch.setattr(
		cli.sys,
		"stdin",
		type("_InteractiveStdin", (), {"isatty": lambda self: True})(),
	)
	monkeypatch.setattr(
		cli,
		"prompt",
		lambda prompt: pytest.fail("help must not start the add prompt"),
	)

	with pytest.raises(SystemExit) as error:
		cli.main(["task", "add", help_flag])

	assert error.value.code == 0
	output = capsys.readouterr()
	assert output.err == ""
	assert "usage: progress task add" in output.out
	assert "--slug" in output.out


def test_add_prompt_skips_arguments_already_supplied(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "tsk_test"}
	prompts: list[str] = []
	prompt_values = iter(
		["Task title", "Task overview", "Task purpose", "Task contract", *([""] * 7)]
	)

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_add(self, **arguments):
			return data

	def fake_input(prompt: str) -> str:
		prompts.append(prompt)
		return next(prompt_values)

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	monkeypatch.setattr(cli, "render", lambda command, data: "")
	monkeypatch.setattr(cli, "prompt", fake_input)
	monkeypatch.setattr(
		cli.sys,
		"stdin",
		type("_InteractiveStdin", (), {"isatty": lambda self: True})(),
	)

	assert (
		cli.main(
			[
				"task",
				"add",
				"--slug",
				"supplied-slug",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = capsys.readouterr()
	assert prompts[0] == "title: "
	assert all(prompt != "slug: " for prompt in prompts)
	assert "--slug" not in output.out
	assert ") Display title" in output.out
	assert output.out.startswith("\n")
	assert output.out.count("\n\n") == len(prompts) - 1


def test_missing_add_arguments_keep_argparse_error_on_non_tty_stdin(
	capsys, monkeypatch
) -> None:
	monkeypatch.setattr(
		cli.sys,
		"stdin",
		type("_NonInteractiveStdin", (), {"isatty": lambda self: False})(),
	)

	assert cli.main(["chunk", "add"]) == 2

	output = capsys.readouterr()
	assert output.out == ""
	assert _stderr_error_message(output.err) == (
		"the following arguments are required: --task, --title, --description"
	)


def test_json_mode_keeps_missing_add_arguments_non_interactive(
	capsys, monkeypatch
) -> None:
	monkeypatch.setattr(
		cli.sys,
		"stdin",
		type("_InteractiveStdin", (), {"isatty": lambda self: True})(),
	)
	monkeypatch.setattr(
		cli,
		"prompt",
		lambda prompt: pytest.fail("JSON mode must not prompt"),
	)

	assert cli.main(["task", "add", "--json"]) == 2

	response = json.loads(capsys.readouterr().out)
	assert response == {
		"ok": False,
		"error": {
			"code": "usage",
			"message": (
				"the following arguments are required: "
				"--slug, --title, --overview, --purpose, --contract"
			),
			"details": {},
		},
	}


def test_other_add_commands_do_not_prompt(capsys, monkeypatch) -> None:
	monkeypatch.setattr(
		cli.sys,
		"stdin",
		type("_InteractiveStdin", (), {"isatty": lambda self: True})(),
	)
	monkeypatch.setattr(
		cli,
		"prompt",
		lambda prompt: pytest.fail("release add must not prompt"),
	)

	assert cli.main(["release", "add"]) == 2

	assert _stderr_error_message(capsys.readouterr().err) == (
		"the following arguments are required: --slug, --title, --overview"
	)


def test_progress_error_renders_a_failed_status_on_stderr(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_complete(self, task_id: str) -> None:
			raise ProgressError("task cannot complete while it has pending chunks")

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"complete",
				"tsk_test",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 1
	)

	output = capsys.readouterr()

	assert output.out == ""
	assert _stderr_error_message(output.err) == (
		"task cannot complete while it has pending chunks"
	)

	plain_err = render_module._ANSI_ESCAPE_PATTERN.sub("", output.err)

	assert plain_err.startswith(("x Error ", "× Error "))


def test_task_clean_passes_force_to_the_write_store(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"removed_count": 0, "removed": [], "blocked": [], "releases_removed": []}
	arguments_seen: dict[str, object] = {}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_clean(self, *, force):
			arguments_seen["force"] = force
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"clean",
				"--force",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen == {"force": True}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize("release_arguments", [["--release", ""], ["--release"]])
def test_task_move_passes_empty_release_as_unassigned(
	tmp_path: Path, monkeypatch, capsys, release_arguments: list[str]
) -> None:
	data = {"id": "tsk_test", "release_id": None, "position": 2}
	arguments_seen: dict[str, object] = {}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_move(self, task_id, **arguments):
			arguments_seen["task_id"] = task_id
			arguments_seen.update(arguments)
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"move",
				"tsk_" + "t" * 22,
				*release_arguments,
				"--after",
				"tsk_" + "a" * 22,
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen == {
		"task_id": "tsk_" + "t" * 22,
		"release_id": "",
		"before_task_id": None,
		"after_task_id": "tsk_" + "a" * 22,
	}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	("error", "expected_code"),
	[
		(AlreadyExistsError("release already exists"), "already-exists"),
		(DuplicateDependencyError("dependency was repeated"), "duplicate-dependency"),
	],
)
def test_write_errors_use_the_stable_json_error_envelope(
	tmp_path: Path, monkeypatch, capsys, error, expected_code: str
) -> None:
	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def release_add(self, **arguments):
			raise error

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"release",
				"add",
				"--slug",
				"release",
				"--title",
				"Release",
				"--overview",
				"Release overview",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 1
	)

	response = json.loads(capsys.readouterr().out)
	assert response["ok"] is False
	assert response["error"]["code"] == expected_code


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
			"release_id": "rel_test",
			"title": "Read surface",
			"status": "in-progress",
			"overview": "Read the current task.",
			"status_reason": "Waiting for a decision",
			"position": 2,
		},
		"chunk": {
			"id": "chk_test",
			"task_id": "tsk_test",
			"title": "CLI output",
			"description": "Render readable output.",
			"status": "active",
			"position": 1,
		},
		"dependency_ids": ["tsk_dependency"],
		"task_rank": 2,
		"task_total": 7,
		"chunk_rank": 1,
		"chunk_total": 3,
		"hint_command": "progress chunk complete chk_test",
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self, *, include_position_totals=False):
			assert include_position_totals is True
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db")]) == 0

	output = capsys.readouterr()

	assert output.err == ""
	assert output.out.startswith("\n")
	assert output.out.endswith("\n\n")
	assert "Project" in output.out and "Agents" in output.out
	assert "Task" in output.out and "Read surface" in output.out
	assert "in progress" in output.out and "task 2 / 7 for release" in output.out
	assert "Info" in output.out and "progress task get tsk_test" in output.out
	assert "Blocking reason" in output.out and "Waiting for a decision" in output.out
	assert "Dependency IDs" in output.out and "tsk_dependency" in output.out
	assert "Chunk" in output.out and "chunk 1 / 3 for task" in output.out
	assert "progress chunk get chk_test" in output.out
	assert "Next action" not in output.out


@pytest.mark.parametrize(
	("status", "expected_tone"),
	[
		("blocked", "danger"),
		("done", "success"),
		("needs-decision", "warning"),
		("pending", "muted"),
		("ready", "muted"),
		("in-progress", "info"),
	],
)
def test_human_next_colours_status_by_result_type(
	monkeypatch, status: str, expected_tone: str
) -> None:
	calls = []

	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		calls.append((value, tone, weight))
		return value

	monkeypatch.setattr(render_module, "render_span", fake_span)

	render_module._render_next_position_line("task", status, 2, 3, "release")

	assert calls[0][1] == expected_tone


def test_row_group_requests_72_column_wrap(monkeypatch) -> None:
	rows = [{"label": "Overview", "value": "A long task overview."}]
	calls: list[tuple[str, dict[str, object]]] = []

	def fake_render(renderer: str, data: dict[str, object]) -> str:
		calls.append((renderer, data))
		return "rendered"

	monkeypatch.setattr(style_module, "render_generic", fake_render)

	assert style_module.row_group(rows) == "rendered"

	assert calls == [("row-group", {"rows": rows, "wrapWidth": 72})]


def test_divider_passes_border_colour_to_cli_style(monkeypatch) -> None:
	calls: list[tuple[str, int | None, str | None, str | None]] = []

	def fake_divider(
		label: str,
		divider_width: int | None,
		divider_colour: str | None,
		label_colour: str | None,
	) -> str:
		calls.append((label, divider_width, divider_colour, label_colour))
		return "divider"

	monkeypatch.setattr(style_module, "render_divider", fake_divider)

	assert style_module.divider(divider_width=72, divider_colour="border") == "divider"

	assert calls == [("", 72, "border", None)]


def test_human_task_list_groups_rows_and_renders_hints(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [
			{
				"id": "tsk_ready",
				"title": "Ready task",
				"status": "ready",
				"release_id": "rel_first",
				"release_title": "First release",
			},
			{
				"id": "tsk_unassigned",
				"title": "Unassigned task",
				"status": "ready",
				"release_id": None,
			},
			{
				"id": "tsk_done",
				"title": "Done task",
				"status": "done",
				"release_id": "rel_second",
				"release_title": "Second release",
			},
			{
				"id": "tsk_blocked",
				"title": "Blocked task",
				"status": "blocked",
				"release_id": "rel_first",
				"release_title": "First release",
			},
		],
		"limit": 4,
		"offset": 0,
		"has_more": True,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def task_list(self, status, limit, offset, *, include_release_titles):
			assert status is None
			assert limit == 4
			assert offset == 0
			assert include_release_titles is True
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"task",
				"list",
				"--limit",
				"4",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = capsys.readouterr()

	assert output.err == ""
	assert output.out.startswith("\n")
	assert output.out.endswith("\n\n")
	assert output.out.count("progress task get TASK_ID") == 1
	assert "Next action: View a task with" in output.out
	assert "Title" in output.out
	assert "Status" in output.out
	assert "ID" in output.out
	assert output.out.index("First release") < output.out.index("Ready task")
	assert output.out.index("Ready task") < output.out.index("Blocked task")
	assert output.out.index("Second release") < output.out.index("Done task")
	assert "Unassigned" in output.out
	assert "Unassigned task" in output.out
	assert "! Blocked task" in output.out
	assert "(tsk_ready)" in output.out
	assert "(tsk_done)" in output.out
	assert "i Hint: View a task with" not in output.out
	assert "i Hint: More results: use --offset 4." in output.out


def test_task_list_uses_release_priority_for_json_and_table_output(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	database_path = tmp_path / "progress.db"
	project = Project(
		"prj_" + "p" * 22,
		"agents",
		"Agent configuration",
		"2026-01-01T00:00:00+00:00",
	)
	database = Database(database_path)
	with database.transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			(project.id, project.slug, project.name, project.created_at),
		)

	monkeypatch.setattr(ProjectStore, "current", lambda self, path=None: project)
	writer = WriteStore(database)
	later_release = writer.release_add(
		"project-review",
		"Project review",
		overview="Review the project.",
		status="planned",
		position=4,
	)
	active_release = writer.release_add(
		"progress-cli",
		"Progress CLI",
		overview="Improve the progress CLI.",
		status="active",
		position=1,
	)
	later_task = writer.task_add(
		"project-review-context",
		"Project review context",
		overview="Review context.",
		purpose="Review project context.",
		contract="Review context contract.",
		release_id=later_release["id"],
		position=1,
	)
	active_task = writer.task_add(
		"progress-cli-read-parity",
		"Progress CLI read parity",
		overview="Keep read commands aligned.",
		purpose="Keep CLI reads aligned.",
		contract="Keep read ordering aligned.",
		release_id=active_release["id"],
		position=1,
	)
	unassigned_task = writer.task_add(
		"unassigned-task",
		"Unassigned task",
		overview="An unassigned task.",
		purpose="Check unassigned ordering.",
		contract="Unassigned tasks use the final queue bucket.",
		position=1,
	)

	assert cli.main(["task", "list", "--json", "--database", str(database_path)]) == 0
	json_response = json.loads(capsys.readouterr().out)
	json_ids = [item["id"] for item in json_response["data"]["items"]]

	assert json_ids == [active_task["id"], later_task["id"], unassigned_task["id"]]

	assert cli.main(["task", "list", "--database", str(database_path)]) == 0
	human_output = capsys.readouterr().out

	assert human_output.index("Progress CLI read parity") < human_output.index(
		"Project review context"
	)
	assert human_output.index("Project review context") < human_output.index(
		"Unassigned task"
	)


def test_chunk_list_renders_status_rows_and_descriptions(monkeypatch) -> None:
	status_calls: list[tuple[str, str, str]] = []

	def fake_status(result_type: str, label: str = "", detail: str = "") -> str:
		status_calls.append((result_type, label, detail))
		return f"{result_type}:{label}" if label else result_type

	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, *args, **kwargs: value,
	)
	monkeypatch.setattr(render_module, "render_status", fake_status)
	monkeypatch.setattr(
		render_module,
		"render_hint",
		lambda message: pytest.fail(f"unexpected hint: {message}"),
	)

	active_description = "The active chunk description."
	pending_description = "The pending chunk description."
	long_identifier = "chk_QByCeE0lGXmeVEbAF7oFKg"
	output = render_module._render_list(
		"chunk list",
		{
			"items": [
				{
					"id": "chk_done",
					"title": "Done output",
					"status": "done",
					"description": "Done descriptions are hidden.",
				},
				{
					"id": "chk_done_other",
					"title": "Other done output",
					"status": "done",
				},
				{
					"id": long_identifier,
					"title": "Active output",
					"status": "active",
					"description": active_description,
				},
				{
					"id": "chk_active_other",
					"title": "Other active output",
					"status": "active",
				},
				{
					"id": "chk_pending",
					"title": "Pending output",
					"status": "pending",
					"description": pending_description,
				},
				{
					"id": "chk_pending_other",
					"title": "Other pending output",
					"status": "pending",
				},
			],
			"has_more": False,
		},
	)

	assert output == (
		"Chunks\n\n"
		"success:done  Done output · chk_done\n"
		"success:done  Other done output · chk_done_other\n\n"
		f"info:active  Active output · {long_identifier}\n\n"
		f"{active_description}\n\n"
		"info:active  Other active output · chk_active_other\n\n"
		"– Pending output · chk_pending\n\n"
		f"{pending_description}\n\n"
		"– Other pending output · chk_pending_other"
	)
	assert "Done descriptions are hidden." not in output
	assert any(long_identifier in line for line in output.splitlines())
	assert "Description" not in output
	assert (
		"success:done  Done output · chk_done\n"
		"success:done  Other done output · chk_done_other"
	) in output
	assert status_calls == [
		("success", "done", ""),
		("success", "done", ""),
		("info", "active", ""),
		("info", "active", ""),
	]


@pytest.mark.parametrize("description", [None, ""])
def test_chunk_list_omits_empty_descriptions(description, monkeypatch) -> None:
	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, *args, **kwargs: value,
	)
	monkeypatch.setattr(
		render_module,
		"render_status",
		lambda result_type, label="", detail="": result_type,
	)

	output = render_module._render_list(
		"chunk list",
		{
			"items": [
				{
					"id": "chk_test",
					"title": "Render output",
					"status": "ready",
					"description": description,
				}
			],
			"has_more": False,
		},
	)

	assert output == "Chunks\n\n– Render output · chk_test"
	assert "Description" not in output


def test_chunk_list_renders_pagination_without_hint(monkeypatch) -> None:
	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, *args, **kwargs: value,
	)
	monkeypatch.setattr(
		render_module,
		"render_status",
		lambda result_type, label="", detail="": result_type,
	)
	monkeypatch.setattr(
		render_module,
		"render_hint",
		lambda message: pytest.fail(f"unexpected hint: {message}"),
	)

	output = render_module._render_list(
		"chunk list",
		{
			"items": [],
			"offset": 0,
			"limit": 1,
			"has_more": True,
		},
	)

	assert output == "Chunks\n\nMore results: use --offset 1."


def test_task_list_action_styles_embedded_commands(monkeypatch) -> None:
	spans: list[tuple[str, str, str | None]] = []

	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		spans.append((value, tone, weight))
		return f"<{value}>"

	monkeypatch.setattr(render_module, "render_span", fake_span)
	monkeypatch.setattr(
		render_module, "render_labelled_line", lambda label, message: message
	)

	output = render_module._render_task_list({"items": [], "has_more": False})

	assert output == (
		"View a task with <progress task get TASK_ID>; reorder with "
		"<progress task move TASK_ID --before/--after TASK_ID>."
	)
	assert spans == [
		("progress task get TASK_ID", "info", "bold"),
		(
			"progress task move TASK_ID --before/--after TASK_ID",
			"info",
			"bold",
		),
	]


@pytest.mark.parametrize(
	("command", "data", "expected_rows"),
	[
		(
			"task get",
			{
				"id": "tsk_test",
				"project_id": "prj_test",
				"slug": "test-task",
				"release_id": "",
				"title": "Test task",
				"overview": "A task overview.",
				"purpose": "A task purpose.",
				"contract": "A task contract.",
				"files": "src/task.py",
				"acceptance_criteria": "Task output is readable.",
				"verification": "Run focused tests.",
				"risks": "Low risk.",
				"status": "ready",
				"status_reason": None,
				"position": 2,
				"created_at": "2026-08-21T10:00:00+00:00",
				"started_at": "2026-08-21T10:05:00+00:00",
				"completed_at": "",
				"updated_at": "2026-08-21T10:10:00+00:00",
			},
			[
				{"label": "ID", "value": "tsk_test"},
				{"label": "Slug", "value": "test-task"},
				{"label": "Title", "value": "Test task"},
				{"label": "Project ID", "value": "prj_test"},
				{"label": "Overview", "value": "A task overview."},
				{"label": "Purpose", "value": "A task purpose."},
				{"label": "Contract", "value": "A task contract."},
				{"label": "Files", "value": "src/task.py"},
				{
					"label": "Acceptance criteria",
					"value": "Task output is readable.",
				},
				{"label": "Verification", "value": "Run focused tests."},
				{"label": "Risks", "value": "Low risk."},
				{"label": "Status", "value": "ready"},
				{"label": "Position", "value": "2"},
				{"label": "Created at", "value": "2026-08-21T10:00:00+00:00"},
				{"label": "Started at", "value": "2026-08-21T10:05:00+00:00"},
				{"label": "Updated at", "value": "2026-08-21T10:10:00+00:00"},
			],
		),
		(
			"chunk get",
			{
				"id": "chk_test",
				"description": "A chunk description.",
				"status": "pending",
			},
			[{"label": "Description", "value": "A chunk description."}],
		),
	],
)
def test_object_planning_fields_use_row_group(
	command: str,
	data: dict[str, object],
	expected_rows: list[dict[str, str]],
	monkeypatch,
) -> None:
	groups: list[list[dict[str, str]]] = []
	divider_calls: list[dict[str, object]] = []

	def fake_row_group(rows: list[dict[str, str]]) -> str:
		groups.append(rows)
		label_width = max(len(row["label"]) for row in rows)
		return "\n".join(
			f"{row['label'].ljust(label_width)}  {row['value']}" for row in rows
		)

	def fake_divider(**kwargs: object) -> str:
		divider_calls.append(kwargs)
		return "Muted divider"

	monkeypatch.setattr(render_module, "render_row_group", fake_row_group)
	monkeypatch.setattr(render_module, "render_divider", fake_divider)

	output = render_module.render(command, data)

	if command == "task get":
		label_width = max(len(row["label"]) for row in expected_rows)
		expected_output = "\nMuted divider\n".join(
			f"{row['label'].ljust(label_width)}  {row['value']}"
			for row in expected_rows
		)

		assert output == f"\n{expected_output}\n\n"
		assert groups == [expected_rows]
		assert divider_calls == [
			{"divider_width": label_width + 2 + 72, "divider_colour": "border"}
		]
	else:
		assert "Description  A chunk description." in output
		assert groups == [expected_rows]
		assert divider_calls == []


@pytest.mark.parametrize(
	("rendered_rows", "rows", "expected_message"),
	[
		(
			"Unexpected output",
			[{"label": "ID", "value": "tsk_test"}],
			"cli-style row-group output did not start with a row label",
		),
		(
			"ID     tsk_test",
			[
				{"label": "ID", "value": "tsk_test"},
				{"label": "Status", "value": "ready"},
			],
			"cli-style row-group output did not contain every row",
		),
	],
)
def test_split_task_get_row_group_rejects_invalid_cli_style_output(
	rendered_rows: str,
	rows: list[dict[str, str]],
	expected_message: str,
) -> None:
	with pytest.raises(ValueError, match=expected_message):
		render_module._split_task_get_row_group(
			rendered_rows,
			rows,
			label_width=6,
		)


def test_task_get_row_group_keeps_wrapped_values_with_one_divider_call(
	monkeypatch,
) -> None:
	rows = [
		{"label": "ID", "value": "tsk_test"},
		{"label": "Overview", "value": "A wrapped overview."},
		{"label": "Status", "value": "ready"},
	]
	divider_calls: list[dict[str, object]] = []

	def fake_row_group(rows: list[dict[str, str]]) -> str:
		return "\n".join(
			[
				"ID        tsk_test",
				"Overview  A wrapped",
				"          overview.",
				"Status    ready",
			]
		)

	def fake_divider(**kwargs: object) -> str:
		divider_calls.append(kwargs)
		return "Muted divider"

	monkeypatch.setattr(render_module, "render_row_group", fake_row_group)
	monkeypatch.setattr(render_module, "render_divider", fake_divider)

	assert render_module._render_task_get_row_group(rows) == (
		"ID        tsk_test\n"
		"Muted divider\n"
		"Overview  A wrapped\n"
		"          overview.\n"
		"Muted divider\n"
		"Status    ready"
	)
	assert divider_calls == [{"divider_width": 82, "divider_colour": "border"}]


def test_task_get_field_order_matches_task_dataclass() -> None:
	expected_fields = {field.name for field in dataclasses.fields(Task)}

	assert set(render_module._TASK_GET_FIELD_ORDER) == expected_fields


def test_human_next_renders_done_chunk_with_success_status(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"id": "prj_test", "slug": "agents", "name": "Agents"},
		"task": {
			"id": "tsk_test",
			"release_id": "rel_test",
			"title": "Read surface",
			"status": "ready",
			"position": 1,
		},
		"chunk": {
			"id": "chk_test",
			"task_id": "tsk_test",
			"title": "CLI output",
			"description": "Render readable output.",
			"status": "done",
			"position": 1,
		},
		"task_total": 1,
		"task_rank": 1,
		"chunk_total": 2,
		"chunk_rank": 1,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self, *, include_position_totals=False):
			assert include_position_totals is True
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db")]) == 0

	output = capsys.readouterr()

	assert "done" in output.out
	assert "chunk 1 / 2 for task" in output.out
	assert "progress chunk get chk_test" in output.out


def test_human_task_clean_renders_counts_and_kept_task_details() -> None:
	data = {
		"removed_count": 2,
		"removed": [
			{"id": "tsk_removed", "title": "Old completed task"},
			{"id": "tsk_removed_two", "title": "Another completed task"},
		],
		"blocked": [
			{
				"id": "tsk_kept",
				"title": "Task with history",
				"notes": [
					{"type": "discovery", "body": "Keep this discovery."},
					{"type": "decision", "body": "Keep this decision."},
				],
				"dependencies": [
					{
						"task_id": "tsk_kept",
						"depends_on_task_id": "tsk_dependency",
						"other_task_id": "tsk_dependency",
						"other_task_title": "Dependency task",
						"direction": "depends_on",
					},
					{
						"task_id": "tsk_required",
						"depends_on_task_id": "tsk_kept",
						"other_task_id": "tsk_required",
						"other_task_title": "Dependent task",
						"direction": "required_by",
					},
				],
			},
			{
				"id": "tsk_kept_two",
				"title": "Second kept task",
				"notes": [],
				"dependencies": [
					{
						"task_id": "tsk_kept_two",
						"depends_on_task_id": "tsk_dependency_two",
						"other_task_id": "tsk_dependency_two",
						"other_task_title": "Second dependency",
						"direction": "depends_on",
					}
				],
			},
		],
		"releases_removed": [{"id": "rel_removed", "title": "Old release"}],
	}

	output = render_module.render("task clean", data)

	assert "2 tasks removed" in output
	assert "2 tasks kept" in output
	assert "Old completed task (tsk_removed)" not in output
	assert "Hint  Tasks with notes or dependencies are kept" in output
	assert "Kept task  Task with history (tsk_kept)" in output
	assert "Reason     1 discovery note, 1 decision note, 2 dependencies" in output
	assert "Discovery note\nKeep this discovery." in output
	assert "Decision note\nKeep this decision." in output
	assert "Dependency\nDepends on: Dependency task (tsk_dependency)" in output
	assert "Required by: Dependent task (tsk_required)" in output
	second_task_start = output.index("Second kept task (tsk_kept_two)")
	first_task_end = output.index("Second kept task", output.index("Task with history"))
	assert "\n\n" in output[output.index("Task with history") : first_task_end]
	assert "Reason     1 dependency" in output[second_task_start:]
	assert (
		"Dependency\nDepends on: Second dependency (tsk_dependency_two)"
		in output[second_task_start:]
	)
	assert "1 release removed" in output
	assert "Removed release  Old release (rel_removed)" in output
	assert "Blocked" not in output


def test_human_task_clean_always_shows_zero_counts() -> None:
	output = render_module.render(
		"task clean",
		{"removed_count": 0, "removed": [], "blocked": [], "releases_removed": []},
	)

	assert "0 tasks removed" in output
	assert "0 tasks kept" in output
	assert "0 releases removed" in output
	assert "Hint" not in output


def test_human_task_clean_uses_summary_colours_and_muted_labels(monkeypatch) -> None:
	calls = []

	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		calls.append((value, tone, weight))
		return value

	monkeypatch.setattr(render_module, "render_span", fake_span)

	render_module.render(
		"task clean",
		{
			"removed_count": 1,
			"removed": [],
			"blocked": [
				{
					"id": "tsk_kept",
					"title": "Kept task",
					"notes": [{"type": "discovery", "body": "Note."}],
					"dependencies": [
						{
							"other_task_id": "tsk_other",
							"other_task_title": "Other task",
							"direction": "depends_on",
						}
					],
				}
			],
			"releases_removed": [],
		},
	)

	assert calls[0] == ("1 task removed", "success", "normal")
	assert calls[1] == ("1 task kept", "warning", "normal")
	assert all(tone == "muted" for _, tone, _ in calls[2:])
	assert all(tone not in {"failed", "info"} for _, tone, _ in calls)


def test_human_task_clean_separates_kept_task_blocks() -> None:
	data = {
		"removed_count": 0,
		"removed": [],
		"blocked": [
			{
				"id": "tsk_one",
				"title": "First task",
				"notes": [{"type": "discovery", "body": "First note."}],
				"dependencies": [],
			},
			{
				"id": "tsk_two",
				"title": "Second task",
				"notes": [{"type": "decision", "body": "Second note."}],
				"dependencies": [],
			},
		],
		"releases_removed": [],
	}

	output = render_module.render("task clean", data)
	first_task_start = output.index("First task")
	second_task_start = output.index("Second task")

	assert "\n\n" in output[first_task_start:second_task_start]
	assert "  discovery note" not in output
	assert "  decision note" not in output
	assert "x " not in output
	assert "× " not in output


@pytest.mark.parametrize(
	("status", "result_type"),
	[
		("in-progress", "info"),
		("blocked", "failed"),
		("needs-decision", "warning"),
		("done", "success"),
	],
)
def test_task_rows_use_distinct_cli_style_results(
	status: str, result_type: str, monkeypatch
) -> None:
	monkeypatch.setattr(
		render_module,
		"render_status",
		lambda rendered_type, label: f"{rendered_type}:{label}",
	)
	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, *args, **kwargs: value,
	)

	row = render_module._render_task_item(
		{"id": "tsk_test", "title": "Task", "status": status}
	)

	assert _status_result_type(status) == result_type
	assert row["status"] == f"{result_type}:{status}"
	assert row["title"] == ("! Task" if status == "blocked" else "Task")
	assert row["id"] == "(tsk_test)"


def test_json_task_list_does_not_request_release_titles(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [{"id": "tsk_test", "title": "Read surface", "status": "ready"}],
		"limit": 50,
		"offset": 0,
		"has_more": False,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def task_list(self, status, limit, offset, *, include_release_titles):
			assert status is None
			assert limit == 50
			assert offset == 0
			assert include_release_titles is False
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"task",
				"list",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	assert capsys.readouterr().out == (
		'{"ok":true,"data":{"items":[{"id":"tsk_test","title":"Read surface",'
		'"status":"ready"}],"limit":50,"offset":0,"has_more":false}}\n'
	)


def test_human_release_list_keeps_trailing_blank_line(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [{"id": "rel_test", "title": "First release", "status": "active"}],
		"limit": 1,
		"offset": 0,
		"has_more": False,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def release_list(self, limit, offset):
			assert limit == 1
			assert offset == 0
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"release",
				"list",
				"--limit",
				"1",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = capsys.readouterr()

	assert output.err == ""
	assert output.out.startswith("\n")
	assert output.out.endswith("\n\n")
	assert "Releases" in output.out
	assert "First release" in output.out


def test_json_write_success_uses_the_changed_object_shape(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"id": "tsk_test",
		"project_id": "prj_test",
		"slug": "write-surface",
		"title": "Write surface",
		"status": "ready",
	}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_add(self, **arguments):
			assert arguments["slug"] == "write-surface"
			assert arguments["title"] == "Write surface"
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"add",
				"--slug",
				"write-surface",
				"--title",
				"Write surface",
				"--overview",
				"Write surface overview",
				"--purpose",
				"Write surface purpose",
				"--contract",
				"Write surface contract",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	("command_arguments", "flag"),
	[
		pytest.param(
			["task", "add", "--slug", "task", "--title", "Task"],
			"--overview",
			id="task-overview",
		),
		pytest.param(
			[
				"task",
				"add",
				"--slug",
				"task",
				"--title",
				"Task",
				"--overview",
				"Task overview",
				"--contract",
				"Task contract",
			],
			"--purpose",
			id="task-purpose",
		),
		pytest.param(
			[
				"task",
				"add",
				"--slug",
				"task",
				"--title",
				"Task",
				"--overview",
				"Task overview",
				"--purpose",
				"Task purpose",
			],
			"--contract",
			id="task-contract",
		),
		pytest.param(
			["release", "add", "--slug", "release", "--title", "Release"],
			"--overview",
			id="release-overview",
		),
		pytest.param(
			["chunk", "add", "--task", "tsk_test", "--title", "Chunk"],
			"--description",
			id="chunk-description",
		),
	],
)
def test_add_rejects_an_omitted_planning_field(
	tmp_path: Path,
	capsys,
	command_arguments: list[str],
	flag: str,
) -> None:
	database_path = tmp_path / "db"

	assert (
		cli.main(
			[
				*command_arguments,
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 2
	)

	result = json.loads(capsys.readouterr().out)

	assert result["ok"] is False
	assert result["error"]["code"] == "usage"
	assert flag in result["error"]["message"]
	assert database_path.exists() is False


@pytest.mark.parametrize(
	("command_arguments", "flag"),
	[
		pytest.param(
			[
				"task",
				"add",
				"--slug",
				"task",
				"--title",
				"Task",
				"--overview",
				" \t",
			],
			"--overview",
			id="task-overview",
		),
		pytest.param(
			[
				"task",
				"add",
				"--slug",
				"task",
				"--title",
				"Task",
				"--overview",
				"Task overview",
				"--purpose",
				" \t",
				"--contract",
				"Task contract",
			],
			"--purpose",
			id="task-purpose",
		),
		pytest.param(
			[
				"task",
				"add",
				"--slug",
				"task",
				"--title",
				"Task",
				"--overview",
				"Task overview",
				"--purpose",
				"Task purpose",
				"--contract",
				" \t",
			],
			"--contract",
			id="task-contract",
		),
		pytest.param(
			[
				"release",
				"add",
				"--slug",
				"release",
				"--title",
				"Release",
				"--overview",
				" \t",
			],
			"--overview",
			id="release-overview",
		),
		pytest.param(
			[
				"chunk",
				"add",
				"--task",
				"tsk_test",
				"--title",
				"Chunk",
				"--description",
				" \t",
			],
			"--description",
			id="chunk-description",
		),
	],
)
def test_add_rejects_a_whitespace_only_planning_field(
	tmp_path: Path,
	capsys,
	command_arguments: list[str],
	flag: str,
) -> None:
	database_path = tmp_path / "db"

	assert (
		cli.main(
			[
				*command_arguments,
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 2
	)

	result = json.loads(capsys.readouterr().out)

	assert result["ok"] is False
	assert result["error"]["code"] == "usage"
	assert f"{flag} must not be empty" in result["error"]["message"]
	assert database_path.exists() is False


@pytest.mark.parametrize(
	("command_arguments", "flag"),
	[
		pytest.param(
			["task", "edit", "tsk_test", "--clear-overview"],
			"--clear-overview",
			id="task-overview",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--clear-purpose"],
			"--clear-purpose",
			id="task-purpose",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--clear-contract"],
			"--clear-contract",
			id="task-contract",
		),
		pytest.param(
			["chunk", "edit", "chk_test", "--clear-description"],
			"--clear-description",
			id="chunk-description",
		),
		pytest.param(
			["release", "edit", "rel_test", "--clear-overview"],
			"--clear-overview",
			id="release-overview",
		),
	],
)
def test_edit_rejects_removed_clear_flags(
	tmp_path: Path,
	capsys,
	command_arguments: list[str],
	flag: str,
) -> None:
	database_path = tmp_path / "db"

	assert (
		cli.main(
			[
				*command_arguments,
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 2
	)

	result = json.loads(capsys.readouterr().out)

	assert result["ok"] is False
	assert result["error"]["code"] == "usage"
	assert flag in result["error"]["message"]
	assert database_path.exists() is False


@pytest.mark.parametrize(
	("command_arguments", "flag"),
	[
		pytest.param(
			["task", "edit", "tsk_test", "--overview", ""],
			"--overview",
			id="task-overview-empty",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--purpose", " \t"],
			"--purpose",
			id="task-purpose-whitespace",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--contract", ""],
			"--contract",
			id="task-contract-empty",
		),
		pytest.param(
			["chunk", "edit", "chk_test", "--description", " \t"],
			"--description",
			id="chunk-description-whitespace",
		),
		pytest.param(
			["release", "edit", "rel_test", "--overview", ""],
			"--overview",
			id="release-overview-empty",
		),
	],
)
def test_edit_rejects_blank_planning_text(
	tmp_path: Path,
	capsys,
	command_arguments: list[str],
	flag: str,
) -> None:
	database_path = tmp_path / "db"

	assert (
		cli.main(
			[
				*command_arguments,
				"--database",
				str(database_path),
				"--json",
			]
		)
		== 2
	)

	result = json.loads(capsys.readouterr().out)

	assert result["ok"] is False
	assert result["error"]["code"] == "usage"
	assert f"{flag} must not be empty" in result["error"]["message"]
	assert database_path.exists() is False


def test_task_edit_dispatches_values_and_optional_clear_flags(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "tsk_test", "overview": "Updated"}
	arguments_seen: dict[str, object] = {}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_edit(self, task_id, **arguments):
			arguments_seen["task_id"] = task_id
			arguments_seen.update(arguments)
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"edit",
				"tsk_test",
				"--overview",
				"Updated",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen == {
		"task_id": "tsk_test",
		"overview": "Updated",
		"purpose": None,
		"contract": None,
		"files": None,
		"acceptance_criteria": None,
		"verification": None,
		"risks": None,
		"clear_files": False,
		"clear_acceptance_criteria": False,
		"clear_verification": False,
		"clear_risks": False,
	}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_chunk_edit_dispatches_description(
	tmp_path: Path,
	monkeypatch,
	capsys,
) -> None:
	data = {"id": "chk_test", "description": "Updated"}
	arguments_seen: dict[str, object] = {}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def chunk_edit(self, chunk_id, **arguments):
			arguments_seen["chunk_id"] = chunk_id
			arguments_seen.update(arguments)
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"chunk",
				"edit",
				"chk_test",
				"--description",
				"Updated",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen == {
		"chunk_id": "chk_test",
		"description": "Updated",
	}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_human_write_output_includes_a_next_command(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"id": "chk_test",
		"task_id": "tsk_test",
		"position": 1,
		"title": "First chunk",
		"description": "Implement it.",
		"status": "pending",
		"started_at": None,
		"completed_at": None,
	}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def chunk_add(self, **arguments):
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"chunk",
				"add",
				"--task",
				"tsk_test",
				"--title",
				"First chunk",
				"--description",
				"Implement it.",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	assert "Next: progress task start tsk_test" in capsys.readouterr().out


def test_human_task_complete_output_is_concise(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"id": "tsk_test",
		"slug": "dependency",
		"title": "Dependency",
		"status": "done",
		"unblocked_tasks": [{"id": "tsk_dependent", "title": "Dependent"}],
	}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_complete(self, task_id):
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"complete",
				"tsk_test",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = capsys.readouterr().out
	plain_lines = [
		render_module._ANSI_ESCAPE_PATTERN.sub("", line) for line in output.splitlines()
	]

	assert len(plain_lines) == 2
	assert plain_lines[0].endswith("Completed task Dependency")
	# Inequality after stripping ANSI proves the line carried styling.
	assert plain_lines[0] != "Completed task Dependency"
	assert plain_lines[1] == "Next: progress next"
	assert "tsk_test" not in output
	assert "Dependent" not in output


def test_human_chunk_complete_output_is_concise(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"id": "chk_test",
		"task_id": "tsk_parent",
		"title": "CLI output",
		"status": "done",
	}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def chunk_complete(self, chunk_id):
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"chunk",
				"complete",
				"chk_test",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = capsys.readouterr().out
	plain_lines = [
		render_module._ANSI_ESCAPE_PATTERN.sub("", line) for line in output.splitlines()
	]

	assert len(plain_lines) == 2
	assert plain_lines[0].endswith("Completed chunk CLI output")
	# Inequality after stripping ANSI proves the line carried styling.
	assert plain_lines[0] != "Completed chunk CLI output"
	assert plain_lines[1] == "Next: progress next"
	assert "chk_test" not in output
	assert "tsk_parent" not in output


def test_project_init_dispatches_to_the_nested_project_command(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "prj_test", "slug": "agents", "name": "Agents"}

	class _Project:
		def to_dict(self):
			return data

	class _ProjectStore:
		def __init__(self, database) -> None:
			pass

		def init(self, slug, name):
			assert (slug, name) == ("agents", "Agents")
			return _Project()

	monkeypatch.setattr(cli, "ProjectStore", _ProjectStore)

	assert (
		cli.main(
			[
				"project",
				"init",
				"--slug",
				"agents",
				"--name",
				"Agents",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}
