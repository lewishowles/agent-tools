import json
from pathlib import Path
import subprocess

import pytest
from agents_progress.database import Database
import agents_progress.render as render_module
import agents_progress.style as style_module
from agents_progress import __version__, cli
from agents_progress.errors import (
	AlreadyExistsError,
	DuplicateDependencyError,
	ProgressError,
)
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
	assert output.out == "\n" + cli.build_parser().format_help()


def test_version_prints_the_styled_package_version(capsys) -> None:
	assert cli.main(["--version"]) == 0

	output = capsys.readouterr()
	plain_output = render_module._ANSI_ESCAPE_PATTERN.sub("", output.out)

	assert output.err == ""
	assert plain_output in (
		f"Version: {__version__}\n",
		f"i Version: {__version__}\n",
	)


def test_version_json_uses_the_standard_envelope(capsys) -> None:
	assert cli.main(["--json", "--version"]) == 0

	output = capsys.readouterr()

	assert output.err == ""
	assert json.loads(output.out) == {"ok": True, "data": {"version": __version__}}


@pytest.mark.parametrize(
	("command", "expected_choices"),
	[
		("project", "{init,attach,current}"),
		("release", "{add,move,list,get,remove,rename,edit,complete}"),
		(
			"task",
			"{add,move,dependency,remove,clean,rename,edit,start,complete,block,unblock,get,list}",
		),
		("chunk", "{add,move,start,complete,remove,rename,edit,get,list}"),
		("discovery", "{add,list,remove}"),
		("decision", "{add,list,remove}"),
		("context", "{get,set}"),
		("task dependency", "{add,remove}"),
	],
)
def test_group_without_subcommand_prints_its_help_and_succeeds(
	capsys, command: str, expected_choices: str
) -> None:
	arguments = command.split()

	assert cli.main(arguments) == 0

	output = capsys.readouterr()

	assert output.err == ""
	assert f"progress {command}" in output.out
	assert expected_choices in output.out

	with pytest.raises(SystemExit) as help_exit:
		cli.main([*arguments, "-h"])

	help_output = capsys.readouterr()

	assert help_exit.value.code == 0
	assert help_output.err == ""
	assert output.out == help_output.out


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
	assert commands["release remove"]["flags"] == [
		{"names": ["release_id"], "required": True},
		{"names": ["--force"], "required": False},
		{"names": ["--json"], "required": False},
		{"names": ["--database"], "required": False},
	]
	assert commands["task remove"]["flags"] == [
		{"names": ["task_id"], "required": True},
		{"names": ["--force"], "required": False},
		{"names": ["--json"], "required": False},
		{"names": ["--database"], "required": False},
	]
	assert commands["discovery add"]["flags"][:2] == [
		{"names": ["--release"], "required": False},
		{"names": ["--task"], "required": False},
	]
	assert commands["discovery add"]["required_one_of"] == ["--release", "--task"]
	assert commands["discovery add"]["flags"][2] == {
		"names": ["body"],
		"required": True,
	}
	assert commands["task add"]["flags"][4] == {
		"names": ["--contract-step"],
		"required": True,
	}
	assert commands["task add"]["flags"][10] == {
		"names": ["--release", "--release-id"],
		"required": False,
	}
	assert commands["release list"]["flags"][-5:] == [
		{"names": ["--all"], "required": False},
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

	output = capsys.readouterr().out

	assert output.startswith("\n")
	assert output.endswith("\n\n")
	assert expected_output in output


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
			["discovery", "list", "--release", "rel_" + "r" * 22],
			"discovery_list",
		),
		(
			["decision", "list", "--task", "tsk_" + "t" * 22],
			"decision_list",
		),
		(
			["decision", "list", "--release", "rel_" + "r" * 22],
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
				"--contract-step",
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
		(
			[
				"release",
				"move",
				"rel_" + "r" * 22,
				"--before",
				"rel_" + "b" * 22,
			],
			"release_move",
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
			["discovery", "add", "--release", "rel_" + "r" * 22, "A", "discovery"],
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
			["decision", "add", "--release", "rel_" + "r" * 22, "A", "decision"],
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


@pytest.mark.parametrize(
	("arguments", "method_name", "expected_arguments", "expected_keywords"),
	[
		(
			[
				"release",
				"add",
				"--slug",
				"release",
				"--title",
				"Release",
				"--overview",
				"Overview",
				"--purpose",
				"Purpose",
				"--risks",
				"Risks",
				"--out-of-scope",
				"First",
				"--out-of-scope",
				"Second",
			],
			"release_add",
			(),
			{
				"slug": "release",
				"title": "Release",
				"overview": "Overview",
				"purpose": "Purpose",
				"risks": "Risks",
				"out_of_scope": ["First", "Second"],
				"status": "planned",
				"position": None,
			},
		),
		(
			[
				"release",
				"edit",
				"rel_" + "r" * 22,
				"--purpose",
				"Updated purpose",
				"--risks",
				"Updated risks",
				"--out-of-scope",
				"Replacement",
				"--out-of-scope",
				"Second replacement",
			],
			"release_edit",
			("rel_" + "r" * 22,),
			{
				"overview": None,
				"purpose": "Updated purpose",
				"risks": "Updated risks",
				"out_of_scope": ["Replacement", "Second replacement"],
				"clear_purpose": False,
				"clear_risks": False,
				"clear_out_of_scope": False,
			},
		),
		(
			[
				"release",
				"edit",
				"rel_" + "r" * 22,
				"--purpose",
				"Updated purpose",
			],
			"release_edit",
			("rel_" + "r" * 22,),
			{
				"overview": None,
				"purpose": "Updated purpose",
				"risks": None,
				"out_of_scope": None,
				"clear_purpose": False,
				"clear_risks": False,
				"clear_out_of_scope": False,
			},
		),
		(
			[
				"release",
				"edit",
				"rel_" + "r" * 22,
				"--clear-purpose",
				"--clear-risks",
				"--clear-out-of-scope",
			],
			"release_edit",
			("rel_" + "r" * 22,),
			{
				"overview": None,
				"purpose": None,
				"risks": None,
				"out_of_scope": None,
				"clear_purpose": True,
				"clear_risks": True,
				"clear_out_of_scope": True,
			},
		),
	],
)
def test_release_planning_options_dispatch_with_their_values(
	tmp_path: Path,
	monkeypatch,
	capsys,
	arguments: list[str],
	method_name: str,
	expected_arguments: tuple[str, ...],
	expected_keywords: dict[str, object],
) -> None:
	data = {"id": "obj_test"}
	calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def handler(*positional_arguments, **keyword_arguments):
				calls.append((name, positional_arguments, keyword_arguments))
				return data

			return handler

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert cli.main([*arguments, "--database", str(tmp_path / "db"), "--json"]) == 0

	assert calls == [(method_name, expected_arguments, expected_keywords)]
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	("arguments", "method_name"),
	[
		(
			["release", "remove", "rel_first", "rel_second"],
			"release_remove",
		),
		(
			["release", "complete", "rel_first", "rel_second"],
			"release_complete",
		),
		(
			["task", "remove", "tsk_first", "tsk_second"],
			"task_remove",
		),
		(
			["task", "complete", "tsk_first", "tsk_second"],
			"task_complete",
		),
		(
			["chunk", "remove", "chk_first", "chk_second"],
			"chunk_remove",
		),
		(
			["chunk", "complete", "chk_first", "chk_second"],
			"chunk_complete",
		),
	],
)
def test_multi_id_write_commands_dispatch_a_list_and_return_a_json_list(
	tmp_path: Path, monkeypatch, capsys, arguments: list[str], method_name: str
) -> None:
	ids = arguments[-2:]
	data = [{"id": identifier} for identifier in ids]
	calls: list[tuple[str, tuple[object, ...]]] = []

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def handler(*positional_arguments, **keyword_arguments):
				calls.append((name, positional_arguments))
				return data

			return handler

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

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

	assert calls == [(method_name, (ids,))]
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	"json_mode",
	[
		pytest.param(False, id="human"),
		pytest.param(True, id="json"),
	],
)
def test_multi_id_write_failure_names_the_failing_id(
	tmp_path: Path, monkeypatch, capsys, json_mode: bool
) -> None:
	failing_id = "tsk_" + "f" * 22

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_complete(self, task_ids: list[str]) -> None:
			raise ProgressError(f"task {task_ids[-1]} failed")

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	arguments = [
		"task",
		"complete",
		"tsk_" + "a" * 22,
		failing_id,
		"--database",
		str(tmp_path / "db"),
	]
	if json_mode:
		arguments.append("--json")

	assert cli.main(arguments) == 1

	output = capsys.readouterr()
	if json_mode:
		assert output.err == ""
		response = json.loads(output.out)
		assert response["ok"] is False
		assert failing_id in response["error"]["message"]
	else:
		assert output.out == ""
		assert failing_id in _stderr_error_message(output.err)


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
			"Task contract step",
			"",
			"",
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
		"contract": ["Task contract step"],
		"files": None,
		"split_rationale": None,
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
		"contract-step: ",
		"contract-step: ",
		"file: ",
		"split-rationale: ",
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
			"Non-empty task contract step",
			"Optional file covered by the task; press Enter to skip",
			"Optional reason for splitting the task; press Enter to skip",
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
			"--contract-step",
			"--file",
			"--split-rationale",
			"--acceptance-criteria",
			"--verification",
			"--risks",
			"--release/--release-id",
			"--depends-on/--dependency",
			"--position",
		)
	)
	assert output.out.count("press Enter to skip") == 8
	# render is stubbed empty here, so main() adds only its blank-line frame and
	# the Next hint after the guided prompts. Check spacing on the prompt section.
	prompt_section, _, next_hint = output.out.partition("\n\n\n\nNext: ")
	assert next_hint
	assert prompt_section.startswith("\n")
	assert prompt_section.count("\n\n") == len(prompts) - 1


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
		[
			"Task title",
			"Task overview",
			"Task purpose",
			"Task contract step",
			*([""] * 9),
		]
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
	# render is stubbed empty here, so main() adds only its blank-line frame and
	# the Next hint after the guided prompts. Check spacing on the prompt section.
	prompt_section, _, next_hint = output.out.partition("\n\n\n\nNext: ")
	assert next_hint
	assert prompt_section.startswith("\n")
	assert prompt_section.count("\n\n") == len(prompts) - 1


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
				"--slug, --title, --overview, --purpose, --contract-step"
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

	assert plain_err.startswith("\n")
	assert plain_err[1:].startswith(("x Error ", "× Error "))


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


def test_release_move_passes_relative_target_to_write_store(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "rel_test", "position": 2}
	arguments_seen: dict[str, object] = {}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def release_move(self, release_id, **arguments):
			arguments_seen["release_id"] = release_id
			arguments_seen.update(arguments)
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"release",
				"move",
				"rel_" + "r" * 22,
				"--after",
				"rel_" + "a" * 22,
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen == {
		"release_id": "rel_" + "r" * 22,
		"before_release_id": None,
		"after_release_id": "rel_" + "a" * 22,
	}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.mark.parametrize(
	"relative_arguments",
	[
		pytest.param([], id="neither"),
		pytest.param(
			[
				"--before",
				"rel_" + "b" * 22,
				"--after",
				"rel_" + "a" * 22,
			],
			id="both",
		),
	],
)
def test_release_move_requires_exactly_one_relative_target(
	tmp_path: Path, capsys, relative_arguments: list[str]
) -> None:
	assert (
		cli.main(
			[
				"release",
				"move",
				"rel_" + "r" * 22,
				*relative_arguments,
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 2
	)

	output = capsys.readouterr()

	assert output.out == ""
	assert _stderr_error_message(output.err) == (
		"release move requires exactly one of --before or --after"
	)


@pytest.mark.parametrize(
	"owner_arguments",
	[
		[],
		[
			"--task",
			"tsk_" + "t" * 22,
			"--release",
			"rel_" + "r" * 22,
		],
	],
)
def test_note_add_requires_exactly_one_owner(
	tmp_path: Path, capsys, owner_arguments: list[str]
) -> None:
	assert (
		cli.main(
			[
				"discovery",
				"add",
				*owner_arguments,
				"Discovery",
				"body",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 2
	)

	output = capsys.readouterr()
	assert output.out == ""
	assert "--release" in output.err
	assert "--task" in output.err


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
	assert "Blocking reason" in output.out and "Waiting for a decision" in output.out
	assert "ID" in output.out and "tsk_test" in output.out
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
	assert "! Blocked task" not in output.out
	assert "(tsk_ready)" not in output.out
	assert "(tsk_done)" not in output.out
	assert "tsk_ready" in output.out
	assert "tsk_done" in output.out
	assert output.out.index("First release") < output.out.index("Next action")
	assert output.out.index("Unassigned task") < output.out.index("Next action")
	assert output.out.index("More results: use --offset 4.") < output.out.index(
		"Next action"
	)
	assert "i Hint: View a task with" not in output.out
	assert "i Hint: More results: use --offset 4." in output.out


def test_human_chunk_list_includes_task_header(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [{"id": "chk_test", "title": "First chunk", "status": "pending"}],
		"limit": 50,
		"offset": 0,
		"has_more": False,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def chunk_list(self, task_id, limit, offset):
			assert (task_id, limit, offset) == ("tsk_test", 50, 0)
			return data

		def task_get(self, task_id):
			assert task_id == "tsk_test"
			return {"id": "tsk_test", "title": "Progress task"}

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"chunk",
				"list",
				"--task",
				"tsk_test",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Progress task · tsk_test\n\nChunks" in output
	assert output.index("Progress task · tsk_test") < output.index("Chunks")
	assert output.index("Chunks") < output.index("First chunk")


def test_human_note_list_shows_body_and_owner(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [
			{
				"id": "nte_task",
				"task_id": "tsk_task",
				"release_id": None,
				"type": "discovery",
				"body": "Task discovery.",
			},
			{
				"id": "nte_release",
				"task_id": None,
				"release_id": "rel_release",
				"type": "discovery",
				"body": "Release discovery.",
			},
		],
		"limit": 50,
		"offset": 0,
		"has_more": False,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def discovery_list(self, task_id, limit, offset, *, release_id):
			assert task_id is None
			assert release_id is None
			assert (limit, offset) == (50, 0)
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"discovery",
				"list",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Task discovery.\ntask tsk_task" in output
	assert "Release discovery.\nrelease rel_release" in output
	assert "nte_task" not in output


@pytest.mark.parametrize(
	("command", "empty_label"),
	[("discovery", "No discovery notes."), ("decision", "No decision notes.")],
)
def test_human_empty_note_list_shows_an_empty_state(
	tmp_path: Path, monkeypatch, capsys, command: str, empty_label: str
) -> None:
	data = {"items": [], "limit": 50, "offset": 0, "has_more": False}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			return lambda *arguments, **keyword_arguments: data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				command,
				"list",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert empty_label in output


def test_json_chunk_list_does_not_add_task_header(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [{"id": "chk_test", "title": "First chunk", "status": "pending"}],
		"limit": 50,
		"offset": 0,
		"has_more": False,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def chunk_list(self, task_id, limit, offset):
			return data

		def task_get(self, task_id):
			pytest.fail("JSON chunk list must not read the task")

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"chunk",
				"list",
				"--task",
				"tsk_test",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_search_dispatches_parsed_arguments(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [
			{
				"type": "task",
				"id": "tsk_search",
				"title": "Search task",
				"status": "done",
				"snippets": {"overview": "A button result."},
			}
		],
		"limit": 7,
		"offset": 3,
		"has_more": False,
	}
	calls = []

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def search(self, term, fields, status, limit, offset):
			calls.append((term, fields, status, limit, offset))
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)
	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, tone="info", weight=None: (
			f"<{value}>" if weight == "bold" else value
		),
	)

	assert (
		cli.main(
			[
				"search",
				"button",
				"--in",
				"overview",
				"--in",
				"files",
				"--status",
				"done",
				"--limit",
				"7",
				"--offset",
				"3",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = capsys.readouterr()

	assert output.err == ""
	assert calls == [("button", ["overview", "files"], "done", 7, 3)]
	assert "  overview: A <button> result." in output.out


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
		contract=["Review context contract."],
		release_id=later_release["id"],
		position=1,
	)
	active_task = writer.task_add(
		"progress-cli-read-parity",
		"Progress CLI read parity",
		overview="Keep read commands aligned.",
		purpose="Keep CLI reads aligned.",
		contract=["Keep read ordering aligned."],
		release_id=active_release["id"],
		position=1,
	)
	unassigned_task = writer.task_add(
		"unassigned-task",
		"Unassigned task",
		overview="An unassigned task.",
		purpose="Check unassigned ordering.",
		contract=["Unassigned tasks use the final queue bucket."],
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
	span_calls: list[tuple[str, str, str | None]] = []

	def fake_status(result_type: str, label: str = "", detail: str = "") -> str:
		status_calls.append((result_type, label, detail))
		return f"{result_type}:{label}" if label else result_type

	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		span_calls.append((value, tone, weight))
		return value

	monkeypatch.setattr(render_module, "render_span", fake_span)
	monkeypatch.setattr(render_module, "render_status", fake_status)
	monkeypatch.setattr(
		render_module,
		"render_hint",
		lambda message: pytest.fail(f"unexpected hint: {message}"),
	)

	active_description = "The active chunk description.\nThis line is hidden."
	pending_description = "The pending chunk description.\nThis line is hidden too."
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
		"Done output\n"
		"success:done · chk_done\n\n"
		"Other done output\n"
		"success:done · chk_done_other\n\n"
		f"Active output\ninfo:active · {long_identifier}\n\n"
		f"{active_description.splitlines()[0]}\n\n"
		"Other active output\ninfo:active · chk_active_other\n\n"
		"Pending output\nskipped:pending · chk_pending\n\n"
		f"{pending_description.splitlines()[0]}\n\n"
		"Other pending output\nskipped:pending · chk_pending_other"
	)
	assert "Done descriptions are hidden." not in output
	assert "This line is hidden." not in output
	assert "This line is hidden too." not in output
	assert any(long_identifier in line for line in output.splitlines())
	assert "Description" not in output
	assert "Done output\nsuccess:done · chk_done" in output
	assert status_calls == [
		("success", "done", ""),
		("success", "done", ""),
		("info", "active", ""),
		("info", "active", ""),
		("skipped", "pending", ""),
		("skipped", "pending", ""),
	]
	assert ("Done output", "text", "normal") in span_calls
	assert ("Active output", "text", "normal") in span_calls
	assert ("Pending output", "text", "normal") in span_calls
	assert ("·", "muted", "normal") in span_calls
	assert (long_identifier, "muted", "normal") in span_calls
	assert (active_description.splitlines()[0], "muted", "normal") in span_calls
	assert (pending_description.splitlines()[0], "muted", "normal") in span_calls


def test_search_renders_task_and_chunk_rows_with_highlighted_snippets(
	monkeypatch,
) -> None:
	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		return f"<{value}>" if weight == "bold" else value

	monkeypatch.setattr(render_module, "render_span", fake_span)
	monkeypatch.setattr(
		render_module, "render_hint", lambda message: f"Hint: {message}"
	)
	monkeypatch.setattr(
		render_module,
		"render_labelled_line",
		lambda label, message: f"{label}: {message}",
	)

	output = render_module._render_list(
		"search",
		{
			"term": "button",
			"items": [
				{
					"type": "task",
					"id": "tsk_task",
					"title": "Search task",
					"status": "in-progress",
					"snippets": {
						"overview": "A Button option with button support.",
						"files": ["packages/Button.py", "src/button.ts"],
					},
				},
				{
					"type": "chunk",
					"id": "chk_chunk",
					"title": "Read chunk",
					"status": "active",
					"task_title": "Search task",
					"snippets": {"description": "Implement the BUTTON query."},
				},
			],
			"limit": 2,
			"offset": 0,
			"has_more": True,
		},
	)

	assert "Task" in output
	assert "in progress" in output
	assert "Search task" in output
	assert "tsk_task" in output
	assert "Chunk" in output
	assert "active" in output
	assert "Read chunk" in output
	assert "chk_chunk" in output
	assert "parent: Search task" in output
	assert "  overview: A <Button> option with <button> support." in output
	assert "  files: packages/<Button>.py" in output
	assert "  files: src/<button>.ts" in output
	assert "  description: Implement the <BUTTON> query." in output
	assert "Hint: More results: use --offset 2." in output
	assert "Next action: View a task with" in output

	assert render_module._render_list("search", {"items": []}) == "No matches."


def test_skipped_chunks_use_the_skipped_tone_and_pending_group(monkeypatch) -> None:
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

	output = render_module.render(
		"chunk list",
		{
			"items": [
				{"id": "chk_active", "title": "Active output", "status": "active"},
				{
					"id": "chk_skipped",
					"title": "Skipped output",
					"status": "skipped",
				},
				{
					"id": "chk_pending",
					"title": "Pending output",
					"status": "pending",
				},
			],
			"has_more": False,
		},
	)

	assert output == (
		"Chunks\n\n"
		"Active output\ninfo:active · chk_active\n\n"
		"Skipped output\nskipped:pending · chk_skipped\n\n"
		"Pending output\nskipped:pending · chk_pending"
	)
	assert status_calls == [
		("info", "active", ""),
		("skipped", "pending", ""),
		("skipped", "pending", ""),
	]


def test_chunk_list_wraps_titles_before_status_rows(monkeypatch) -> None:
	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, *args, **kwargs: value,
	)
	monkeypatch.setattr(
		render_module,
		"render_status",
		lambda result_type, label="", detail="": label,
	)

	title = "A chunk title that wraps across the terminal width before its status line"
	output = render_module._render_list(
		"chunk list",
		{
			"items": [
				{"id": "chk_test", "title": title, "status": "active"},
			],
			"has_more": False,
		},
	)

	lines = output.splitlines()
	assert lines == [
		"Chunks",
		"",
		"A chunk title that wraps across the terminal width before its status",
		"line",
		"active · chk_test",
	]
	assert all(len(line) <= 72 for line in lines)


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

	assert output == "Chunks\n\nRender output\nskipped · chk_test"
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

	assert output == "Chunks\n\nNo chunks.\n\nMore results: use --offset 1."


def test_task_list_action_styles_embedded_commands(monkeypatch) -> None:
	spans: list[tuple[str, str, str | None]] = []
	tables: list[tuple[list[dict[str, str]], list[dict[str, str]]]] = []

	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		spans.append((value, tone, weight))
		return f"<{value}>"

	monkeypatch.setattr(render_module, "render_span", fake_span)
	monkeypatch.setattr(
		render_module,
		"render_status",
		lambda result_type, label: f"{result_type}:{label}",
	)
	monkeypatch.setattr(
		render_module,
		"render_table",
		lambda columns, rows: tables.append((columns, rows)) or "table",
	)
	monkeypatch.setattr(
		render_module, "render_labelled_line", lambda label, message: message
	)

	output = render_module._render_task_list(
		{
			"items": [{"id": "tsk_test", "title": "Task", "status": "ready"}],
			"has_more": False,
		}
	)

	assert output == (
		"<Unassigned>\n\n"
		"table\n\n"
		"View a task with <progress task get TASK_ID>; reorder with "
		"<progress task move TASK_ID --before/--after TASK_ID>."
	)
	assert spans[:2] == [
		("progress task get TASK_ID", "info", "bold"),
		(
			"progress task move TASK_ID --before/--after TASK_ID",
			"info",
			"bold",
		),
	]
	assert ("Unassigned", "info", "normal") in spans
	assert tables == [
		(
			[
				{"key": "status", "label": "Status"},
				{"key": "title", "label": "Title"},
				{"key": "id", "label": "ID"},
			],
			[
				{
					"id": "<tsk_test>",
					"status": "skipped:ready   ",
					"title": "Task",
				}
			],
		)
	]


def test_task_list_renders_only_an_empty_state(monkeypatch) -> None:
	span_calls: list[tuple[str, str, str | None]] = []

	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		span_calls.append((value, tone, weight))
		return value

	monkeypatch.setattr(render_module, "render_span", fake_span)
	monkeypatch.setattr(
		render_module,
		"render_labelled_line",
		lambda label, message: pytest.fail("unexpected next action"),
	)

	assert (
		render_module._render_task_list({"items": [], "has_more": True}) == "No tasks."
	)
	assert span_calls == [("No tasks.", "muted", "normal")]


def test_chunk_get_renders_one_readable_chunk_view() -> None:
	data = {
		"id": "chk_chunk_view",
		"task_id": "tsk_chunk_view",
		"title": "Readable chunk",
		"status": "skipped",
		"description": "The complete chunk description.\nThe second line remains.",
		"position": 99,
		"started_at": "hidden-started-at",
		"completed_at": "hidden-completed-at",
	}

	output = render_module.render("chunk get", data)
	plain_output = render_module._ANSI_ESCAPE_PATTERN.sub("", output)

	assert plain_output.startswith("Readable chunk")
	assert "Status" in plain_output and "skipped" in plain_output
	assert "Chunk ID" in plain_output and "chk_chunk_view" in plain_output
	assert "Task ID" in plain_output and "tsk_chunk_view" in plain_output
	assert "Description" in plain_output
	assert "The complete chunk description." in plain_output
	assert "The second line remains." in plain_output
	assert "hidden-started-at" not in plain_output
	assert "hidden-completed-at" not in plain_output
	assert plain_output.index("Readable chunk") < plain_output.index("Status")
	assert plain_output.index("Status") < plain_output.index("Description")


def test_chunk_get_tones_the_status_by_its_result_type(monkeypatch) -> None:
	span_calls: list[tuple[str, str, str | None]] = []

	def fake_span(value: str, tone: str = "info", weight: str | None = None) -> str:
		span_calls.append((value, tone, weight))
		return value

	monkeypatch.setattr(render_module, "render_span", fake_span)

	render_module.render(
		"chunk get",
		{
			"id": "chk_toned",
			"task_id": "tsk_toned",
			"title": "Toned chunk",
			"status": "skipped",
			"description": "A description.",
		},
	)

	assert ("skipped", "muted", "bold") in span_calls


def test_chunk_get_routes_to_the_dedicated_chunk_view(monkeypatch) -> None:
	monkeypatch.setattr(
		render_module,
		"_render_object",
		lambda data: pytest.fail("chunk get used the generic renderer"),
	)
	monkeypatch.setattr(render_module, "_render_chunk", lambda chunk: "chunk view")

	assert render_module.render("chunk get", {"id": "chk_test"}) == "chunk view"


def test_task_get_uses_one_readable_task_view() -> None:
	data = {
		"id": "tsk_task_view",
		"project_id": "prj_task_view",
		"release_id": "rel_task_view",
		"slug": "hidden-task-slug",
		"title": "Readable task",
		"status": "needs-decision",
		"status_reason": "Waiting for product input.",
		"overview": "The task overview.",
		"purpose": "The task purpose.",
		"chunks": [
			{
				"id": "chk_first",
				"title": "First chunk",
				"description": "First chunk description.\nHidden first chunk line.",
				"status": "done",
			},
			{
				"id": "chk_second",
				"title": "Second chunk",
				"description": "Second chunk description.\nHidden second chunk line.",
				"status": "pending",
			},
		],
		"split_rationale": "Keep the two chunks independently reviewable.",
		"contract": ["First contract step.", "Second contract step."],
		"files": ["src/task.py", "tests/test_task.py"],
		"acceptance_criteria": "The task view is complete.",
		"verification": "Run the task tests.",
		"risks": "The output may be long.",
		"position": 99,
		"created_at": "hidden-created-at",
		"started_at": "hidden-started-at",
		"completed_at": "hidden-completed-at",
		"updated_at": "hidden-updated-at",
	}

	output = render_module.render("task get", data)
	plain_output = render_module._ANSI_ESCAPE_PATTERN.sub("", output)

	assert plain_output.startswith("Readable task")
	assert "Status" in plain_output and "needs decision" in plain_output
	assert "ID" in plain_output and "tsk_task_view" in plain_output
	assert "Waiting for product input." in plain_output
	assert "Overview" in plain_output and "The task overview." in plain_output
	assert "Purpose" in plain_output and "The task purpose." in plain_output
	assert "Chunks" in plain_output
	assert "First chunk" in plain_output and "Second chunk" in plain_output
	assert "First chunk description." not in plain_output
	assert "Second chunk description." in plain_output
	assert "Hidden first chunk line." not in plain_output
	assert "Hidden second chunk line." not in plain_output
	assert "Split rationale" in plain_output
	assert "First contract step." in plain_output
	assert "Second contract step." in plain_output
	assert "src/task.py" in plain_output and "tests/test_task.py" in plain_output
	assert "Acceptance criteria" in plain_output
	assert "Verification" in plain_output
	assert "Risks" in plain_output
	assert "Project ID" in plain_output and "prj_task_view" in plain_output
	assert "Release ID" in plain_output and "rel_task_view" in plain_output
	assert "hidden-task-slug" not in plain_output
	assert "hidden-created-at" not in plain_output
	assert "hidden-started-at" not in plain_output
	assert "hidden-completed-at" not in plain_output
	assert "hidden-updated-at" not in plain_output
	assert "position" not in plain_output.lower()
	assert plain_output.index("Readable task") < plain_output.index("Status")
	assert plain_output.index("Status") < plain_output.index("Overview")
	assert plain_output.index("Overview") < plain_output.index("Purpose")
	assert plain_output.index("Purpose") < plain_output.index("Chunks")
	assert plain_output.index("Risks") < plain_output.index("Project ID")


def test_release_get_uses_one_readable_release_view() -> None:
	data = {
		"id": "rel_release_view",
		"project_id": "prj_release_view",
		"slug": "hidden-release-slug",
		"title": "Readable release",
		"overview": "The release overview.",
		"purpose": "The release purpose.",
		"risks": "The release risks.",
		"status": "in-progress",
		"out_of_scope": ["First excluded item.", "Second excluded item."],
		"notes": [
			{
				"type": "discovery",
				"body": "The full release note.\nThe second note line.",
				"release_id": "rel_release_view",
				"task_id": None,
			},
		],
		"position": 99,
	}

	output = render_module.render("release get", data)
	plain_output = render_module._ANSI_ESCAPE_PATTERN.sub("", output)

	assert plain_output.startswith("Readable release")
	assert "Status" in plain_output and "in progress" in plain_output
	assert "ID" in plain_output and "rel_release_view" in plain_output
	assert "Overview" in plain_output and "The release overview." in plain_output
	assert "Purpose" in plain_output and "The release purpose." in plain_output
	assert "Risks" in plain_output and "The release risks." in plain_output
	assert "Out of scope" in plain_output
	assert "First excluded item." in plain_output
	assert "Second excluded item." in plain_output
	assert plain_output.count("Notes") == 1
	assert "Release notes" not in plain_output
	assert "release rel_release_view" not in plain_output
	assert "The full release note.\nThe second note line." in plain_output
	assert "hidden-release-slug" not in plain_output
	assert "position" not in plain_output.lower()
	assert plain_output.index("Readable release") < plain_output.index("Status")
	assert plain_output.index("Status") < plain_output.index("Overview")
	assert plain_output.index("Overview") < plain_output.index("Purpose")
	assert plain_output.index("Purpose") < plain_output.index("Risks")
	assert plain_output.index("Risks") < plain_output.index("Out of scope")
	assert plain_output.index("Out of scope") < plain_output.index("Notes")


def test_release_get_omits_empty_optional_sections() -> None:
	output = render_module.render(
		"release get",
		{
			"id": "rel_empty_view",
			"title": "Minimal release",
			"status": "planned",
			"overview": "Overview only.",
			"purpose": None,
			"risks": None,
			"out_of_scope": [],
			"notes": [],
		},
	)
	plain_output = render_module._ANSI_ESCAPE_PATTERN.sub("", output)

	assert "Overview" in plain_output
	assert "Purpose" not in plain_output
	assert "Risks" not in plain_output
	assert "Out of scope" not in plain_output
	assert "Notes" not in plain_output


def test_release_get_routes_to_the_dedicated_release_view(monkeypatch) -> None:
	monkeypatch.setattr(
		render_module,
		"_render_object",
		lambda data: pytest.fail("release get used the generic renderer"),
	)
	monkeypatch.setattr(
		render_module, "_render_release", lambda release: "release view"
	)

	assert render_module.render("release get", {"id": "rel_test"}) == "release view"


def test_json_release_get_includes_planning_fields(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"id": "rel_test",
		"title": "Release",
		"purpose": "Purpose.",
		"risks": "Risks.",
		"out_of_scope": ["Excluded work."],
		"notes": [{"type": "decision", "body": "Decision."}],
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def release_get(self, release_id):
			assert release_id == "rel_test"
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"release",
				"get",
				"rel_test",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_task_get_routes_around_the_generic_object_renderer(monkeypatch) -> None:
	monkeypatch.setattr(
		render_module,
		"_render_object",
		lambda data: pytest.fail("task get used the generic renderer"),
	)
	monkeypatch.setattr(render_module, "_render_task", lambda task: "task view")

	assert render_module.render("task get", {"id": "tsk_test"}) == "task view"


def test_next_reuses_the_task_view_and_keeps_position_and_active_chunk(
	monkeypatch,
) -> None:
	task = {"id": "tsk_test", "status": "ready"}
	chunk = {
		"id": "chk_test",
		"title": "Active chunk",
		"description": "Active chunk description.",
		"status": "active",
	}
	calls = []

	def fake_render_task(task_data: dict[str, object]) -> str:
		calls.append(task_data)
		return "shared task view"

	monkeypatch.setattr(render_module, "_render_task", fake_render_task)

	output = render_module._render_next(
		{
			"project": {"name": "Agents"},
			"task": task,
			"chunk": chunk,
			"task_rank": 2,
			"task_total": 4,
			"chunk_rank": 1,
			"chunk_total": 3,
		}
	)

	assert calls == [task]
	assert "task 2 / 4 for release" in output
	assert "shared task view" in output
	assert "chunk 1 / 3 for task" in output
	assert "Active chunk" in output
	assert "Active chunk description." in output


def test_human_next_renders_release_before_task(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"name": "Agents"},
		"release": {
			"id": "rel_next",
			"title": "Current release",
			"status": "active",
			"overview": "Release overview.",
			"purpose": "Release purpose.",
			"risks": "Release risks.",
			"out_of_scope": ["Excluded work."],
			"notes": [
				{
					"type": "decision",
					"body": "Release decision.",
					"release_id": "rel_next",
					"task_id": None,
				}
			],
		},
		"task": {"id": "tsk_next", "title": "Current task", "status": "ready"},
		"chunk": None,
		"task_rank": 1,
		"task_total": 1,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self, *, include_position_totals=False):
			assert include_position_totals is True
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db")]) == 0

	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Current release" in output
	assert "Release overview." in output
	assert "Release decision." in output
	assert "Current task" in output
	assert output.index("Current release") < output.index("Current task")


def test_human_next_omits_release_without_one(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"name": "Agents"},
		"release": None,
		"task": {"id": "tsk_next", "title": "Current task", "status": "ready"},
		"chunk": None,
		"task_rank": 1,
		"task_total": 1,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self, *, include_position_totals=False):
			assert include_position_totals is True
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db")]) == 0

	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Current task" in output
	assert "Release" not in output


def test_json_next_includes_the_release_data(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"name": "Agents"},
		"release": {
			"id": "rel_next",
			"purpose": "Release purpose.",
			"risks": "Release risks.",
			"out_of_scope": ["Excluded work."],
			"notes": [{"type": "discovery", "body": "Release discovery."}],
		},
		"task": None,
		"chunk": None,
		"dependency_ids": [],
		"hint_command": "progress task list",
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--json", "--database", str(tmp_path / "db")]) == 0

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


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
	assert row["status"] == f"{result_type}:{status}".ljust(16)
	assert row["title"] == "Task"
	assert row["id"] == "tsk_test"


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


def test_json_search_keeps_rows_plain_and_unchanged(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [
			{
				"type": "task",
				"id": "tsk_test",
				"title": "Search task",
				"status": "ready",
				"matched": ["overview"],
				"snippets": {"overview": "A button result."},
			}
		],
		"limit": 50,
		"offset": 0,
		"has_more": False,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def search(self, term, fields, status, limit, offset):
			assert term == "button"
			assert fields is None
			assert status is None
			assert limit == 50
			assert offset == 0
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"search",
				"button",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = capsys.readouterr()

	assert output.err == ""
	assert "\x1b" not in output.out
	assert json.loads(output.out) == {"ok": True, "data": data}


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

		def release_list(self, limit, offset, *, show_all, include_hidden_count):
			assert limit == 1
			assert offset == 0
			assert show_all is False
			assert include_hidden_count is True
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
	assert "(rel_test)" not in output.out
	assert "rel_test" in output.out


def test_release_list_hides_done_releases_without_changing_json_keys(
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
	planned_release = writer.release_add(
		"planned-release",
		"Planned release",
		overview="Plan the release.",
		status="planned",
		position=1,
	)
	active_release = writer.release_add(
		"active-release",
		"Active release",
		overview="Build the release.",
		status="active",
		position=2,
	)
	done_release = writer.release_add(
		"done-release",
		"Done release",
		overview="Completed release.",
		status="done",
		position=3,
	)
	second_done_release = writer.release_add(
		"second-done-release",
		"Second done release",
		overview="Another completed release.",
		status="done",
		position=4,
	)

	assert (
		cli.main(
			[
				"release",
				"list",
				"--limit",
				"2",
				"--json",
				"--database",
				str(database_path),
			]
		)
		== 0
	)

	default_response = json.loads(capsys.readouterr().out)["data"]
	assert set(default_response) == {"items", "limit", "offset", "has_more"}
	assert [item["id"] for item in default_response["items"]] == [
		planned_release["id"],
		active_release["id"],
	]
	assert default_response["has_more"] is False

	assert cli.main(["release", "list", "--database", str(database_path)]) == 0

	human_output = capsys.readouterr().out
	assert "2 completed releases hidden. Use --all to show them." in human_output

	assert (
		cli.main(
			[
				"release",
				"list",
				"--all",
				"--limit",
				"4",
				"--json",
				"--database",
				str(database_path),
			]
		)
		== 0
	)

	all_response = json.loads(capsys.readouterr().out)["data"]
	assert set(all_response) == {"items", "limit", "offset", "has_more"}
	assert [item["id"] for item in all_response["items"]] == [
		planned_release["id"],
		active_release["id"],
		done_release["id"],
		second_done_release["id"],
	]
	assert all_response["has_more"] is False

	assert cli.main(["release", "list", "--all", "--database", str(database_path)]) == 0
	assert "completed release hidden" not in capsys.readouterr().out

	assert (
		cli.main(
			[
				"release",
				"get",
				done_release["id"],
				"--json",
				"--database",
				str(database_path),
			]
		)
		== 0
	)
	assert json.loads(capsys.readouterr().out)["data"]["status"] == "done"


def test_release_list_uses_singular_wording_for_one_hidden_done_release(
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
	writer.release_add(
		"active-release",
		"Active release",
		overview="Build the release.",
		status="active",
		position=1,
	)
	writer.release_add(
		"done-release",
		"Done release",
		overview="Completed release.",
		status="done",
		position=2,
	)

	assert cli.main(["release", "list", "--database", str(database_path)]) == 0

	human_output = capsys.readouterr().out
	assert "1 completed release hidden. Use --all to show them." in human_output
	assert "releases hidden" not in human_output


def test_release_list_does_not_show_a_hidden_count_hint_without_done_releases(
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
	WriteStore(database).release_add(
		"active-release",
		"Active release",
		overview="Build the release.",
		status="active",
	)

	assert cli.main(["release", "list", "--database", str(database_path)]) == 0

	assert "completed release" not in capsys.readouterr().out


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
				"--contract-step",
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
				"--contract-step",
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
			"--contract-step",
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
				"--contract-step",
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
				"--contract-step",
				" \t",
			],
			"--contract-step",
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
			["task", "edit", "tsk_test", "--contract-step", ""],
			"--contract-step",
			id="task-contract-empty",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--split-rationale", ""],
			"--split-rationale",
			id="task-split-rationale-empty",
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
		"split_rationale": None,
		"acceptance_criteria": None,
		"verification": None,
		"risks": None,
		"clear_files": False,
		"clear_split_rationale": False,
		"clear_acceptance_criteria": False,
		"clear_verification": False,
		"clear_risks": False,
	}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_task_add_dispatches_repeatable_contract_steps_and_files(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "tsk_test"}
	arguments_seen: dict[str, object] = {}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_add(self, **arguments):
			arguments_seen.update(arguments)
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
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
				"--contract-step",
				"First step",
				"--contract-step",
				"Second step",
				"--file",
				"src/first.py",
				"--file",
				"src/second.py",
				"--split-rationale",
				"Keep each behaviour slice reviewable",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen["contract"] == ["First step", "Second step"]
	assert arguments_seen["files"] == ["src/first.py", "src/second.py"]
	assert arguments_seen["split_rationale"] == "Keep each behaviour slice reviewable"
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_task_edit_dispatches_repeatable_contract_steps_and_files(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "tsk_test"}
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
				"--contract-step",
				"Updated first",
				"--contract-step",
				"Updated second",
				"--file",
				"src/updated.py",
				"--split-rationale",
				"The chunks have separate review questions",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen == {
		"task_id": "tsk_test",
		"overview": None,
		"purpose": None,
		"contract": ["Updated first", "Updated second"],
		"files": ["src/updated.py"],
		"split_rationale": "The chunks have separate review questions",
		"acceptance_criteria": None,
		"verification": None,
		"risks": None,
		"clear_files": False,
		"clear_split_rationale": False,
		"clear_acceptance_criteria": False,
		"clear_verification": False,
		"clear_risks": False,
	}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_task_edit_dispatches_split_rationale_clear_flags(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {"id": "tsk_test"}
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
				"--clear-files",
				"--clear-split-rationale",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen["files"] is None
	assert arguments_seen["split_rationale"] is None
	assert arguments_seen["clear_files"] is True
	assert arguments_seen["clear_split_rationale"] is True
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_removed_task_contract_and_files_flags_are_rejected(capsys) -> None:
	assert (
		cli.main(
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
				"--contract-step",
				"Current step",
				"--contract",
				"Old",
				"--json",
			]
		)
		== 2
	)
	first = json.loads(capsys.readouterr().out)
	assert first["ok"] is False
	assert first["error"]["message"] == "unrecognized arguments: --contract Old"

	assert cli.main(["task", "edit", "tsk_test", "--files", "old", "--json"]) == 2
	second = json.loads(capsys.readouterr().out)
	assert second["ok"] is False
	assert second["error"]["message"] == "unrecognized arguments: --files old"


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
	render_spans: list[tuple[str, str, str | None]] = []
	next_spans: list[tuple[str, str, str | None]] = []
	data = {
		"id": "tsk_test",
		"slug": "dependency",
		"release_id": "rel_parent",
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
	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, tone="info", weight=None: (
			render_spans.append((value, tone, weight)) or value
		),
	)
	monkeypatch.setattr(
		cli,
		"render_span",
		lambda value, tone="info", weight=None: (
			next_spans.append((value, tone, weight)) or value
		),
	)

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

	assert output.startswith("\n")
	assert output.endswith("\n\nNext: progress next\n")
	assert len(plain_lines) == 5
	assert plain_lines[1].endswith("Completed task Dependency")
	# Inequality after stripping ANSI proves the line carried styling.
	assert plain_lines[1] != "Completed task Dependency"
	assert plain_lines[2] == "Release ID: rel_parent"
	assert plain_lines[4] == "Next: progress next"
	assert ("Release ID: rel_parent", "muted", "normal") in render_spans
	assert ("Next: progress next", "muted", "normal") in next_spans
	assert "tsk_test" not in output
	assert "Dependent" not in output


def test_human_chunk_complete_output_is_concise(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	render_spans: list[tuple[str, str, str | None]] = []
	next_spans: list[tuple[str, str, str | None]] = []
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
	monkeypatch.setattr(
		render_module,
		"render_span",
		lambda value, tone="info", weight=None: (
			render_spans.append((value, tone, weight)) or value
		),
	)
	monkeypatch.setattr(
		cli,
		"render_span",
		lambda value, tone="info", weight=None: (
			next_spans.append((value, tone, weight)) or value
		),
	)

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

	assert output.startswith("\n")
	assert output.endswith("\n\nNext: progress next\n")
	assert len(plain_lines) == 5
	assert plain_lines[1].endswith("Completed chunk CLI output")
	# Inequality after stripping ANSI proves the line carried styling.
	assert plain_lines[1] != "Completed chunk CLI output"
	assert plain_lines[2] == "Task ID: tsk_parent"
	assert plain_lines[4] == "Next: progress next"
	assert ("Task ID: tsk_parent", "muted", "normal") in render_spans
	assert ("Next: progress next", "muted", "normal") in next_spans
	assert "chk_test" not in output


@pytest.mark.parametrize(
	("arguments", "method_name", "expected_lines"),
	[
		(
			["release", "remove", "rel_first", "rel_second"],
			"release_remove",
			["ID: rel_first", "ID: rel_second"],
		),
		(
			["release", "complete", "rel_first", "rel_second"],
			"release_complete",
			["ID: rel_first", "ID: rel_second"],
		),
		(
			["task", "remove", "tsk_first", "tsk_second"],
			"task_remove",
			["ID: tsk_first", "ID: tsk_second"],
		),
		(
			["task", "complete", "tsk_first", "tsk_second"],
			"task_complete",
			["Completed task First", "Completed task Second"],
		),
		(
			["chunk", "remove", "chk_first", "chk_second"],
			"chunk_remove",
			["ID: chk_first", "ID: chk_second"],
		),
		(
			["chunk", "complete", "chk_first", "chk_second"],
			"chunk_complete",
			["Completed chunk First", "Completed chunk Second"],
		),
	],
)
def test_human_multi_id_writes_print_one_line_per_result(
	tmp_path: Path,
	monkeypatch,
	capsys,
	arguments: list[str],
	method_name: str,
	expected_lines: list[str],
) -> None:
	data = [
		{"id": arguments[-2], "title": "First"},
		{"id": arguments[-1], "title": "Second"},
	]

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def handler(*positional_arguments, **keyword_arguments):
				assert name == method_name
				return data

			return handler

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				*arguments,
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	plain_lines = [
		render_module._ANSI_ESCAPE_PATTERN.sub("", line)
		for line in capsys.readouterr().out.splitlines()
	]

	assert len(plain_lines[1:-1]) == len(expected_lines)
	for line, expected in zip(plain_lines[1:-1], expected_lines):
		assert line.endswith(expected)


def test_force_remove_passes_the_flag_and_returns_deleted_json(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	ids = ["tsk_first", "tsk_second"]
	data = [
		{
			"id": identifier,
			"deleted": {
				"chunks": [],
				"notes": [],
				"dependencies": [],
				"tasks": [identifier],
				"out_of_scope": [],
			},
		}
		for identifier in ids
	]
	arguments_seen: dict[str, object] = {}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_remove(self, task_ids, *, force):
			arguments_seen["task_ids"] = task_ids
			arguments_seen["force"] = force
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"remove",
				*ids,
				"--force",
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert arguments_seen == {"task_ids": ids, "force": True}
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_force_remove_human_output_groups_deleted_records(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"id": "tsk_test",
		"deleted": {
			"chunks": ["chk_test"],
			"notes": ["nte_test"],
			"dependencies": ["tsk_other -> tsk_test"],
			"tasks": ["tsk_test"],
			"out_of_scope": ["Out of scope"],
		},
		"unblocked_tasks": ["tsk_ready"],
	}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_remove(self, task_id, *, force):
			assert task_id == "tsk_test"
			assert force is True
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"remove",
				"tsk_test",
				"--force",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	plain_output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "ID: tsk_test" in plain_output
	assert "Chunks: chk_test" in plain_output
	assert "Notes: nte_test" in plain_output
	assert "Dependencies: tsk_other -> tsk_test" in plain_output
	assert "Tasks: tsk_test" in plain_output
	assert "Out of scope: Out of scope" in plain_output
	assert "Unblocked tasks: tsk_ready" in plain_output


def test_human_task_complete_output_omits_null_release_id(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	next_spans: list[tuple[str, str, str | None]] = []
	data = {
		"id": "tsk_test",
		"release_id": None,
		"title": "Unassigned task",
		"status": "done",
	}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_complete(self, task_id):
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	monkeypatch.setattr(
		cli,
		"render_span",
		lambda value, tone="info", weight=None: (
			next_spans.append((value, tone, weight)) or value
		),
	)

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

	assert len(plain_lines) == 4
	assert plain_lines[1].endswith("Completed task Unassigned task")
	assert "Release ID" not in output
	assert plain_lines[3] == "Next: progress next"
	assert ("Next: progress next", "muted", "normal") in next_spans


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
			return _Project(), False

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


@pytest.mark.parametrize("json_output", [False, True])
def test_project_init_reports_the_existing_project(
	tmp_path, monkeypatch, capsys, json_output
) -> None:
	subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
	monkeypatch.chdir(tmp_path)
	database_args = ["--database", str(tmp_path / "progress.db")]
	init_args = ["project", "init", "--slug", "agents", "--name", "Agents"]

	assert cli.main([*init_args, *database_args, "--json"]) == 0
	fresh_output = capsys.readouterr()
	fresh_result = json.loads(fresh_output.out)
	assert fresh_output.err == ""
	assert fresh_result["ok"] is True
	assert set(fresh_result["data"]) == {"id", "slug", "name"}
	assert fresh_result["data"]["slug"] == "agents"
	assert fresh_result["data"]["name"] == "Agents"

	assert cli.main(["project", "current", *database_args]) == 0
	current_output = capsys.readouterr()
	assert current_output.err == ""

	init_args = ["project", "init", "--slug", "other", "--name", "Other project"]
	assert (
		cli.main([*init_args, *database_args, *(["--json"] if json_output else [])])
		== 0
	)
	existing_output = capsys.readouterr()

	assert existing_output.err == ""
	if json_output:
		assert json.loads(existing_output.out) == {
			"ok": True,
			"data": {**fresh_result["data"], "already_initialised": True},
		}
	else:
		assert "Repo already initialised" in existing_output.out
		assert existing_output.out.strip().endswith(current_output.out.strip())
		assert "already_initialised" not in existing_output.out
		assert "Already initialised:" not in existing_output.out
