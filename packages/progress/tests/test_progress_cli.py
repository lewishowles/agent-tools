import json
import subprocess
from pathlib import Path

import pytest

import agents_progress.render as render_module
import agents_progress.style as style_module
from agents_progress import __version__, cli
from agents_progress.database import Database
from agents_progress.errors import (
	AlreadyExistsError,
	DuplicateDependencyError,
	NotFoundError,
	ProgressError,
)
from agents_progress.projects import Project, ProjectStore
from agents_progress.reads import ReadStore
from agents_progress.render import _status_result_type
from agents_progress.writes import WriteStore


def _stderr_error_message(captured_err: str) -> str:
	"""Return the error message without its status marker, label, ANSI codes, or following usage text."""
	stripped = render_module._ANSI_ESCAPE_PATTERN.sub("", captured_err)
	_, _, after_marker = stripped.partition(" ")

	return after_marker.removeprefix("Error ").partition("\nusage: ")[0].rstrip("\n")


@pytest.mark.parametrize(
	"prefix, method_name, render_command, record",
	[
		("prj_", "project_get", "project current", {"name": "Agent tools"}),
		("rel_", "release_get", "release get", {"title": "First release"}),
		("tsk_", "task_get", "task get", {"title": "First task"}),
		("chk_", "chunk_get", "chunk get", {"title": "First chunk"}),
		(
			"nte_",
			"note_get",
			"show note",
			{"type": "discovery", "body": "A useful finding", "task_id": "tsk_owner"},
		),
		(
			"nte_",
			"note_get",
			"show note",
			{"type": "decision", "body": "The chosen path", "task_id": "tsk_owner"},
		),
		(
			"inb_",
			"inbox_get",
			"show inbox",
			{"text": "Review this", "created_at": "2026-01-01T00:00:00+00:00"},
		),
	],
)
@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_show_reads_one_record_from_its_id(
	tmp_path: Path,
	monkeypatch,
	capsys,
	prefix: str,
	method_name: str,
	render_command: str,
	record: dict[str, object],
	json_mode: bool,
) -> None:
	identifier = prefix + "a" * 22
	data = {"id": identifier, **record}
	calls = []

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			assert name == method_name

			def get(identifier_arg):
				calls.append(identifier_arg)
				return data

			return get

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)
	arguments = ["show", identifier, "--database", str(tmp_path / "db")]
	if json_mode:
		arguments.append("--json")

	assert cli.main(arguments) == 0
	output = capsys.readouterr()
	assert calls == [identifier]
	assert output.err == ""
	if json_mode:
		assert json.loads(output.out) == {"ok": True, "data": data}
	else:
		plain_output = render_module._ANSI_ESCAPE_PATTERN.sub("", output.out)
		expected = render_module._ANSI_ESCAPE_PATTERN.sub(
			"", render_module.render(render_command, data)
		)
		assert plain_output.strip() == expected.strip()
		if prefix == "nte_":
			assert (
				plain_output.strip().splitlines()[0]
				== f"{data['type'].capitalize()} note"
			)
			assert str(data["body"]) in plain_output
			assert identifier in plain_output
		if prefix == "inb_":
			assert str(data["text"]) in plain_output
			assert identifier in plain_output


@pytest.mark.parametrize("identifier", ["tsk_short", "xyz_" + "a" * 22])
def test_show_rejects_invalid_ids_before_reading(
	tmp_path: Path, monkeypatch, capsys, identifier: str
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pytest.fail("An invalid ID must not reach the read store")

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(["show", identifier, "--database", str(tmp_path / "db"), "--json"])
		== 1
	)
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid-id"


@pytest.mark.parametrize("prefix", ["prj_", "rel_", "tsk_", "chk_", "nte_", "inb_"])
def test_show_reports_missing_records(
	tmp_path: Path, monkeypatch, capsys, prefix: str
) -> None:
	identifier = prefix + "a" * 22

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def get(identifier_arg):
				raise NotFoundError(
					f"record {identifier_arg} was not found", {"id": identifier_arg}
				)

			return get

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(["show", identifier, "--database", str(tmp_path / "db"), "--json"])
		== 1
	)
	response = json.loads(capsys.readouterr().out)
	assert response["error"]["code"] == "not-found"
	assert response["error"]["details"] == {"id": identifier}


def test_show_requires_an_id(capsys) -> None:
	assert cli.main(["show", "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


@pytest.mark.parametrize(
	"command, explicit_id, selected_kind, method_name",
	[
		(["task", "get"], "tsk_explicit", "task", "task_get"),
		(["chunk", "get"], "chk_explicit", "chunk", "chunk_get"),
		(["chunk", "list"], "tsk_explicit", "task", "chunk_list"),
	],
)
@pytest.mark.parametrize("use_selection", [False, True])
def test_read_commands_resolve_only_omitted_identifiers(
	tmp_path: Path,
	monkeypatch,
	capsys,
	command: list[str],
	explicit_id: str,
	selected_kind: str,
	method_name: str,
	use_selection: bool,
) -> None:
	calls = []
	selected_id = "tsk_selected" if selected_kind == "task" else "chk_selected"
	data = (
		{"items": [], "limit": 50, "offset": 0, "has_more": False}
		if method_name == "chunk_list"
		else {"id": selected_id if use_selection else explicit_id}
	)

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self):
			calls.append(("next",))
			return {"task": {"id": "tsk_selected"}, "chunk": {"id": "chk_selected"}}

		def task_get(self, identifier):
			calls.append(("task_get", identifier))
			if method_name == "chunk_list":
				return {"id": identifier, "title": "Progress task"}

			return data

		def chunk_get(self, identifier):
			calls.append(("chunk_get", identifier))
			return data

		def chunk_list(self, identifier, limit, offset, **kwargs):
			calls.append(("chunk_list", identifier, limit, offset))
			assert kwargs == {"collapse_done_chunks": False, "show_all": False}
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)
	identifier_arguments = (
		[]
		if use_selection
		else (["--task", explicit_id] if method_name == "chunk_list" else [explicit_id])
	)

	assert (
		cli.main(
			[
				*command,
				*identifier_arguments,
				"--database",
				str(tmp_path / "db"),
				"--json",
			]
		)
		== 0
	)

	assert calls == ([("next",)] if use_selection else []) + (
		[
			("chunk_list", selected_id if use_selection else explicit_id, 50, 0),
			("task_get", selected_id if use_selection else explicit_id),
		]
		if method_name == "chunk_list"
		else [(method_name, selected_id if use_selection else explicit_id)]
	)
	expected_data = (
		{
			**data,
			"task": {
				"id": selected_id if use_selection else explicit_id,
				"title": "Progress task",
			},
		}
		if method_name == "chunk_list"
		else data
	)
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": expected_data}


@pytest.mark.parametrize(
	"command, selected_kind",
	[
		(["task", "get"], "task"),
		(["chunk", "get"], "chunk"),
		(["chunk", "list"], "task"),
	],
)
def test_read_commands_hint_when_no_selection_exists(
	tmp_path: Path, monkeypatch, capsys, command: list[str], selected_kind: str
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self):
			return {"task": None, "chunk": None}

		def task_get(self, identifier):
			pytest.fail("No task ID should reach the store")

		def chunk_get(self, identifier):
			pytest.fail("No chunk ID should reach the store")

		def chunk_list(self, identifier, limit, offset):
			pytest.fail("No task ID should reach the store")

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)
	arguments = [*command, "--database", str(tmp_path / "db")]

	assert cli.main([*arguments, "--json"]) == 1
	response = json.loads(capsys.readouterr().out)
	assert response == {
		"ok": False,
		"error": {
			"code": "not-found",
			"message": f"no {selected_kind} is selected",
			"details": {"hint": "Run progress next to see the current selection."},
		},
	}

	assert cli.main(arguments) == 1
	assert "Run progress next to see the current selection." in capsys.readouterr().err


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


def test_inbox_help_names_add_dismiss_and_list(capsys) -> None:
	with pytest.raises(SystemExit) as help_exit:
		cli.main(["inbox", "-h"])

	output = capsys.readouterr()

	assert help_exit.value.code == 0
	assert output.err == ""
	assert "progress inbox" in output.out
	assert "{add,dismiss,list}" in output.out


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
				"progress inbox list",
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
				"progress inbox add",
				"progress discovery add",
				"progress decision add",
			],
			None,
		),
		(
			["get"],
			"get",
			[
				"progress worktree get",
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
			"details": {"usage": cli.build_parser().format_usage()},
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
			"details": {"usage": cli.build_parser().format_usage()},
		},
	}
	assert (
		"Did you mean one of:\n  progress release list" in response["error"]["message"]
	)


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
@pytest.mark.parametrize(
	"arguments, expected_message, expected_usage",
	[
		pytest.param(
			["task", "list", "--bogus"],
			"unrecognized arguments: --bogus",
			"usage: progress task list ",
			id="unknown-flag",
		),
		pytest.param(
			["task", "--bogus"],
			"unrecognized arguments: --bogus",
			"usage: progress task ",
			id="unknown-flag-after-group",
		),
		pytest.param(
			["task", "list", "extra"],
			"unrecognized arguments: extra",
			"usage: progress task list ",
			id="unexpected-argument",
		),
		pytest.param(
			["task", "get", "--database"],
			"argument --database: expected one argument",
			"usage: progress task get ",
			id="missing-argument",
		),
		pytest.param(
			["task", "bogus"],
			"'bogus' is not a command on its own",
			"usage: progress task ",
			id="unknown-nested-action",
		),
		pytest.param(
			["bogus"],
			"invalid choice: 'bogus'",
			"usage: progress ",
			id="unknown-top-level-word",
		),
		pytest.param(
			["chunk", "list", "first", "--task", "second"],
			"give the task either as an argument or with --task",
			"usage: progress chunk list ",
			id="both-task-forms",
		),
		pytest.param(
			["checkout", "detach"],
			"checkout detach needs a PATH or --stale",
			"usage: progress checkout detach ",
			id="checkout-detach-without-selection",
		),
	],
)
def test_usage_errors_show_the_failing_command(
	tmp_path: Path,
	monkeypatch,
	capsys,
	arguments: list[str],
	expected_message: str,
	expected_usage: str,
	json_mode: bool,
) -> None:
	"""Show the failing command's usage after each kind of argument error."""
	monkeypatch.setenv("AGENTS_PROGRESS_DATABASE", str(tmp_path / "progress.db"))
	if json_mode:
		arguments = [*arguments, "--json"]

	assert cli.main(arguments) == 2

	output = capsys.readouterr()
	if json_mode:
		assert output.err == ""
		response = json.loads(output.out)
		assert response["ok"] is False
		assert response["error"]["code"] == "usage"
		assert expected_message in response["error"]["message"]
		usage = response["error"]["details"]["usage"]
	else:
		assert output.out == ""
		stripped_error = render_module._ANSI_ESCAPE_PATTERN.sub("", output.err)
		error_text, separator, usage_text = stripped_error.partition("\nusage: ")
		assert separator
		assert expected_message in error_text
		usage = "usage: " + usage_text

	assert usage.startswith(expected_usage)
	assert usage.endswith("\n")


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


@pytest.mark.parametrize("group", ["chunk", "task", "release"])
def test_update_alias_stays_out_of_group_help(capsys, group: str) -> None:
	with pytest.raises(SystemExit) as exception:
		cli.build_parser().parse_args([group, "--help"])

	help_text = capsys.readouterr().out

	assert exception.value.code == 0
	assert "update" not in help_text
	assert "edit (update)" not in help_text
	assert "{edit,update" not in help_text


def test_commands_json_lists_the_registry_with_required_flags(
	capsys,
) -> None:
	assert cli.main(["commands", "--json"]) == 0

	response = json.loads(capsys.readouterr().out)
	manifest = response["data"]
	commands = {item["path"]: item for item in manifest}

	assert response["ok"] is True
	assert all(not path.endswith(" update") for path in commands)
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
	assert commands["task add"]["flags"][3] == {
		"names": ["--contract-step"],
		"required": True,
	}
	assert commands["task add"]["flags"][7] == {
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

		def next(self, *, task_id=None, include_position_totals=False):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db"), "--json"]) == 0

	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_next_task_flag_selects_the_named_task(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	task_id = "tsk_" + "t" * 22
	data = {"project": {"id": "prj_test"}, "task": {"id": task_id}, "chunk": None}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self, *, task_id: str, include_position_totals: bool):
			assert task_id == "named-task"
			assert include_position_totals is False
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"next",
				"--task",
				"named-task",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_task_start_secondary_flag_reaches_the_write_store(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	task_id = "tsk_" + "t" * 22
	data = {"id": task_id, "status": "in-progress"}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_start(self, selected_id: str, *, secondary: bool):
			assert selected_id == task_id
			assert secondary is True
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			[
				"task",
				"start",
				task_id,
				"--secondary",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)
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
		(
			{
				"findings": [
					{
						"field": "task.split_rationale",
						"id": "tsk_test",
						"noun": "task",
						"title": "Task without a split rationale",
					}
				],
				"ok": False,
			},
			"- task.split_rationale: Task without a split rationale (tsk_test)",
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
		(["context", "get", "--task", "tsk_" + "t" * 22], "context_get"),
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
				"--review-question",
				"Does it work?",
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
		(["inbox", "dismiss", "inb_" + "n" * 22], "inbox_dismiss", "write"),
		(["context", "set", "--current-goal", "Goal"], "context_set", "write"),
		(
			["context", "set", "--task", "tsk_" + "t" * 22, "--current-goal", "Goal"],
			"context_set",
			"write",
		),
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

		def record_checkout(self) -> None:
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


@pytest.mark.parametrize("command", ["get", "set"])
def test_context_commands_pass_the_selected_task_to_the_store(
	tmp_path: Path, monkeypatch, capsys, command: str
) -> None:
	task_id = "tsk_" + "t" * 22
	calls: list[tuple[str, str | None]] = []

	class _ContextStore:
		def __init__(self, database) -> None:
			pass

		def context_get(self, *, task_id: str | None = None):
			calls.append(("get", task_id))
			return {"task_id": task_id}

		def context_set(self, **arguments):
			calls.append(("set", arguments["task_id"]))
			return {"task_id": arguments["task_id"]}

	monkeypatch.setattr(cli, "ReadStore", _ContextStore)
	monkeypatch.setattr(cli, "WriteStore", _ContextStore)
	arguments = ["context", command, "--task", task_id]
	if command == "set":
		arguments.extend(["--current-goal", "Named work"])

	assert cli.main([*arguments, "--database", str(tmp_path / "db"), "--json"]) == 0

	assert calls == [(command, task_id)]
	assert json.loads(capsys.readouterr().out) == {
		"ok": True,
		"data": {"task_id": task_id},
	}


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
			],
			"release_add",
			(),
			{
				"slug": "release",
				"title": "Release",
				"overview": "Overview",
				"status": "planned",
				"position": None,
			},
		),
		(
			[
				"release",
				"edit",
				"rel_" + "r" * 22,
				"--overview",
				"Updated overview",
			],
			"release_edit",
			("rel_" + "r" * 22,),
			{"overview": "Updated overview"},
		),
		(
			[
				"release",
				"update",
				"rel_" + "r" * 22,
				"--overview",
				"Updated overview",
			],
			"release_edit",
			("rel_" + "r" * 22,),
			{"overview": "Updated overview"},
		),
	],
)
def test_release_edit_dispatches_overview(
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


@pytest.mark.parametrize("flag", ["--purpose", "--risks", "--out-of-scope"])
def test_release_removed_flags_are_rejected(tmp_path: Path, capsys, flag: str) -> None:
	arguments = [
		"release",
		"add",
		"--slug",
		"release",
		"--title",
		"Release",
		"--overview",
		"Overview",
		flag,
		"Removed",
		"--database",
		str(tmp_path / "db"),
		"--json",
	]

	assert cli.main(arguments) == 2
	assert "unrecognized arguments" in capsys.readouterr().out


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
				"Overview",
				"--contract-step",
				"Contract",
				"--purpose",
				"Removed",
			],
			"--purpose",
			id="task-add-purpose",
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
				"Overview",
				"--contract-step",
				"Contract",
				"--acceptance-criteria",
				"Removed",
			],
			"--acceptance-criteria",
			id="task-add-acceptance-criteria",
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
				"Overview",
				"--contract-step",
				"Contract",
				"--risks",
				"Removed",
			],
			"--risks",
			id="task-add-risks",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--clear-purpose"],
			"--clear-purpose",
			id="task-edit-purpose",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--clear-acceptance-criteria"],
			"--clear-acceptance-criteria",
			id="task-edit-acceptance-criteria",
		),
		pytest.param(
			["task", "edit", "tsk_test", "--clear-risks"],
			"--clear-risks",
			id="task-edit-risks",
		),
	],
)
def test_task_removed_flags_are_rejected(
	tmp_path: Path, capsys, command_arguments: list[str], flag: str
) -> None:
	arguments = [
		*command_arguments,
		"--database",
		str(tmp_path / "db"),
		"--json",
	]

	assert cli.main(arguments) == 2
	output = capsys.readouterr().out

	assert "unrecognized arguments" in output
	assert flag in output


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
			"Task contract step",
			*([""] * 7),
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
		"contract": ["Task contract step"],
		"files": None,
		"split_rationale": None,
		"verification": "",
		"release_id": None,
		"depends_on": [],
		"position": None,
	}
	assert prompts == [
		"slug: ",
		"title: ",
		"overview: ",
		"contract-step: ",
		"contract-step: ",
		"file: ",
		"split-rationale: ",
		"verification: ",
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
			"Non-empty task contract step",
			"Optional file covered by the task; press Enter to skip",
			(
				"Reason for splitting the task; doctor expects every task "
				"to have one; press Enter to skip"
			),
			"Optional verification instructions; press Enter to skip",
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
			"--contract-step",
			"--file",
			"--split-rationale",
			"--verification",
			"--release/--release-id",
			"--depends-on/--dependency",
			"--position",
		)
	)
	assert output.out.count("press Enter to skip") == 6
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
	prompt_values = iter(
		["tsk_test", "Chunk title", "Chunk description", "Does it work?", ""]
	)

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
		"review_question": "Does it work?",
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
			"Task contract step",
			*([""] * 7),
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
		"the following arguments are required: --task, --title, --description, --review-question"
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
	assert response["ok"] is False
	assert response["error"]["code"] == "usage"
	assert response["error"]["message"] == (
		"the following arguments are required: "
		"--slug, --title, --overview, --contract-step"
	)
	assert response["error"]["details"]["usage"].startswith("usage: progress task add ")


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


@pytest.mark.parametrize(
	("prefix", "method_name"),
	[("tsk_", "task_complete"), ("chk_", "chunk_complete")],
)
def test_complete_dispatches_one_id_to_its_store_method(
	tmp_path: Path, monkeypatch, capsys, prefix: str, method_name: str
) -> None:
	"""Send a task or chunk ID to the matching completion method."""
	identifier = prefix + "a" * 22
	calls = []
	data = {"id": identifier, "title": "Completed"}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def __getattr__(self, name):
			def handler(value):
				calls.append((name, value))
				return data

			return handler

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(["complete", identifier, "--database", str(tmp_path / "db"), "--json"])
		== 0
	)
	assert calls == [(method_name, identifier)]
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


@pytest.fixture
def complete_records(tmp_path: Path, monkeypatch) -> tuple[Path, dict, dict]:
	"""Create a task with a pending chunk in a real project database."""
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
	task = writer.task_add(
		"complete-command",
		"Complete command",
		overview="Complete a task or chunk by ID.",
		contract=["Complete the requested record."],
	)
	chunk = writer.chunk_add(
		task["id"],
		"Implement completion",
		"Complete the chunk.",
		review_question="Does completion work?",
	)

	return database_path, task, chunk


def test_complete_finishes_a_real_chunk_by_id(complete_records, capsys) -> None:
	"""Complete an active chunk through the public command."""
	database_path, task, chunk = complete_records
	writer = WriteStore(Database(database_path))
	writer.task_start(task["id"])

	assert (
		cli.main(["complete", chunk["id"], "--database", str(database_path), "--json"])
		== 0
	)
	assert json.loads(capsys.readouterr().out)["data"]["status"] == "done"
	assert ReadStore(Database(database_path)).chunk_get(chunk["id"])["status"] == "done"


def test_complete_keeps_a_task_with_pending_chunks_unchanged(
	complete_records, capsys
) -> None:
	"""Refuse to complete a task until its pending chunk is done."""
	database_path, task, _chunk = complete_records
	reader = ReadStore(Database(database_path))
	status_before = reader.task_get(task["id"])["status"]

	assert (
		cli.main(["complete", task["id"], "--database", str(database_path), "--json"])
		== 1
	)
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "pending-chunks"
	assert reader.task_get(task["id"])["status"] == status_before


@pytest.mark.parametrize(
	("identifier", "expected_code"),
	[
		("plain", "invalid-id"),
		("tsk_short", "invalid-id"),
		("rel_" + "a" * 22, "wrong-id-type"),
		("prj_" + "a" * 22, "wrong-id-type"),
		("nte_" + "a" * 22, "wrong-id-type"),
	],
)
def test_complete_rejects_invalid_or_unrelated_ids_without_writing(
	tmp_path: Path, monkeypatch, capsys, identifier: str, expected_code: str
) -> None:
	"""Reject malformed and unrelated IDs before opening the write store."""

	class _WriteStore:
		def __init__(self, database) -> None:
			pytest.fail("A rejected ID must not reach the write store")

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(["complete", identifier, "--database", str(tmp_path / "db"), "--json"])
		== 1
	)
	response = json.loads(capsys.readouterr().out)
	assert response["error"]["code"] == expected_code
	if expected_code == "wrong-id-type":
		assert response["error"]["message"] == (
			f"expected a task (tsk_) or chunk (chk_) ID, got {identifier!r}"
		)
		assert response["error"]["details"]["expected_prefixes"] == ["tsk_", "chk_"]
		assert "expected_prefix" not in response["error"]["details"]


def test_complete_requires_exactly_one_id(capsys) -> None:
	"""Require one ID for the public completion command."""
	for arguments in ([], ["tsk_" + "a" * 22, "chk_" + "b" * 22]):
		assert cli.main(["complete", *arguments, "--json"]) == 2
		assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_complete_preserves_not_found_for_an_unknown_valid_id(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	"""Report an unknown valid ID with the existing not-found error."""
	identifier = "tsk_" + "a" * 22

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_complete(self, value):
			assert value == identifier
			raise NotFoundError("task does not exist")

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(["complete", identifier, "--database", str(tmp_path / "db"), "--json"])
		== 1
	)
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "not-found"


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

		def next(self, *, task_id=None, include_position_totals=False):
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
				"queue_number": 1,
				"release_id": "rel_first",
				"release_title": "First release",
			},
			{
				"id": "tsk_blocked",
				"title": "Blocked task",
				"status": "blocked",
				"queue_number": 2,
				"release_id": "rel_first",
				"release_title": "First release",
			},
			{
				"id": "tsk_unassigned",
				"title": "Unassigned task",
				"status": "ready",
				"queue_number": 3,
				"release_id": None,
			},
			{
				"id": "tsk_done",
				"title": "Done task",
				"status": "done",
				"release_id": "rel_second",
				"release_title": "Second release",
			},
		],
		"limit": 4,
		"offset": 0,
		"has_more": True,
		"release_order": [
			{"id": "rel_first", "title": "First release"},
			{"id": None, "title": None},
			{"id": "rel_second", "title": "Second release"},
		],
		"done_counts": {},
		"releases_with_unfinished": {"rel_first", None},
		"status_counts": {"ready": 2, "blocked": 1, "done": 1},
		"next_task": "Ready task",
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def task_list(
			self,
			status,
			limit,
			offset,
			*,
			include_release_titles,
			include_queue_numbers,
			collapse_done_tasks,
			show_all,
		):
			assert status is None
			assert limit == 4
			assert offset == 0
			assert include_release_titles is True
			assert include_queue_numbers is True
			assert collapse_done_tasks is True
			assert show_all is False
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
	assert output.out.index("Blocked task") < output.out.index("Ready task")
	assert output.out.index("Second release") < output.out.index("Done task")
	assert "Unassigned" in output.out
	assert "Unassigned task" in output.out
	assert "! Blocked task" not in output.out
	assert "(tsk_ready)" not in output.out
	assert "(tsk_done)" not in output.out
	assert "tsk_ready" in output.out
	assert "tsk_done" in output.out
	assert output.out.index("Second release") < output.out.index("Unassigned")
	assert output.out.index("Unassigned") < output.out.index("First release")
	assert output.out.index("First release") < output.out.index("Next action")
	assert output.out.index("Unassigned task") < output.out.index("Next action")
	assert output.out.index("More results: use --offset 4.") < output.out.index(
		"Next action"
	)
	assert "i Hint: View a task with" not in output.out
	assert "i Hint: More results: use --offset 4." in output.out


def test_human_task_list_shows_done_counts_and_project_summary() -> None:
	data = {
		"items": [
			{
				"id": "tsk_next",
				"title": "Next task",
				"status": "ready",
				"queue_number": 1,
				"release_id": "rel_next",
			},
		],
		"limit": 50,
		"offset": 0,
		"has_more": False,
		"release_order": [
			{"id": "rel_next", "title": "Next release"},
			{"id": "rel_done", "title": "Finished release"},
		],
		"done_counts": {"rel_next": 2, "rel_done": 3},
		"releases_with_unfinished": {"rel_next"},
		"status_counts": {"ready": 1, "done": 5},
		"next_task": "Next task",
	}

	output = render_module._render_task_list(data)

	assert output.index("Finished release") < output.index("Next release")
	assert output.index("2 done") < output.index("Next task")
	assert output.index("Next action") < output.index(
		"6 tasks: 1 ready, 5 done. Next: Next task."
	)
	assert "3 done" in output


@pytest.fixture
def task_list_pages(tmp_path: Path, monkeypatch) -> Path:
	"""Create two releases split by a page and one release with only done tasks."""
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
	releases = [
		writer.release_add(
			"first", "First release", overview="First.", status="active"
		),
		writer.release_add("second", "Second release", overview="Second."),
		writer.release_add("finished", "Finished release", overview="Finished."),
	]
	done_ids = []
	for release, titles in zip(
		releases,
		(
			("First ready one", "First ready two", "First ready three"),
			("Second ready", "Second done one", "Second done two"),
			("Finished done",),
		),
		strict=True,
	):
		for position, title in enumerate(titles, 1):
			task = writer.task_add(
				title.lower().replace(" ", "-"),
				title,
				overview=title,
				contract=[title],
				release_id=release["id"],
				position=position,
			)
			if "done" in title:
				done_ids.append(task["id"])
	with database.transaction() as connection:
		connection.executemany(
			"UPDATE tasks SET status = 'done' WHERE id = ?",
			[(task_id,) for task_id in done_ids],
		)

	return database_path


def test_human_task_list_done_counts_follow_the_visible_page(
	task_list_pages: Path, capsys
) -> None:
	assert (
		cli.main(["task", "list", "--limit", "2", "--database", str(task_list_pages)])
		== 0
	)
	first_page = capsys.readouterr().out
	assert "First ready one" in first_page
	assert "Second release" not in first_page
	assert "Finished release" not in first_page
	assert "More results: use --offset 2." in first_page

	assert (
		cli.main(
			[
				"task",
				"list",
				"--limit",
				"2",
				"--offset",
				"2",
				"--database",
				str(task_list_pages),
			]
		)
		== 0
	)
	final_page = capsys.readouterr().out
	assert "First ready three" in final_page
	assert "Second ready" in final_page
	assert "2 done" in final_page
	assert "Finished release" in final_page
	assert "1 done" in final_page
	assert "More results" not in final_page


def test_human_task_list_all_shows_done_rows_without_counts_or_paging(
	task_list_pages: Path, capsys
) -> None:
	assert cli.main(["task", "list", "--all", "--database", str(task_list_pages)]) == 0
	output = capsys.readouterr().out
	assert "Second done one" in output
	assert "Finished done" in output
	assert "2 done" not in output
	assert "More results" not in output


def test_human_task_list_status_filter_keeps_done_rows(
	task_list_pages: Path, capsys
) -> None:
	assert (
		cli.main(
			["task", "list", "--status", "done", "--database", str(task_list_pages)]
		)
		== 0
	)
	output = capsys.readouterr().out
	assert "Second done one" in output
	assert "Second ready" not in output
	assert "2 done" not in output


@pytest.mark.parametrize("page_flag", ["--limit", "--offset"])
def test_task_list_all_rejects_explicit_paging(
	tmp_path: Path, capsys, page_flag: str
) -> None:
	assert (
		cli.main(
			[
				"task",
				"list",
				"--all",
				page_flag,
				"1",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 2
	)
	assert "--all cannot be used with --limit or --offset" in capsys.readouterr().err


def test_json_task_list_all_removes_page_limit(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def task_list(
			self,
			status,
			limit,
			offset,
			*,
			include_release_titles,
			include_queue_numbers,
			collapse_done_tasks,
			show_all,
		):
			assert show_all is True
			assert collapse_done_tasks is False
			return {"items": [], "limit": None, "offset": 0, "has_more": False}

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			["task", "list", "--all", "--json", "--database", str(tmp_path / "db")]
		)
		== 0
	)
	assert json.loads(capsys.readouterr().out)["data"] == {
		"items": [],
		"limit": None,
		"offset": 0,
		"has_more": False,
	}


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

		def chunk_list(self, task_id, limit, offset, **kwargs):
			assert (task_id, limit, offset) == ("tsk_test", 50, 0)
			assert kwargs == {"collapse_done_chunks": True, "show_all": False}
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


@pytest.mark.parametrize(
	("items", "limit", "offset", "has_more", "expected_body"),
	[
		([], 50, 0, False, "No chunks."),
		(
			[{"id": "chk_test", "title": "First chunk", "status": "pending"}],
			1,
			1,
			True,
			"More results: use --offset 2.",
		),
	],
)
def test_human_chunk_list_keeps_task_header_for_empty_and_paginated_results(
	tmp_path: Path, monkeypatch, capsys, items, limit, offset, has_more, expected_body
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def chunk_list(self, task_id, requested_limit, requested_offset, **kwargs):
			assert (task_id, requested_limit, requested_offset) == (
				"tsk_test",
				limit,
				offset,
			)
			assert kwargs == {"collapse_done_chunks": True, "show_all": False}
			return {
				"items": items,
				"limit": limit,
				"offset": offset,
				"has_more": has_more,
			}

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
				"--limit",
				str(limit),
				"--offset",
				str(offset),
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Progress task · tsk_test\n\nChunks" in output
	assert output.index("Progress task · tsk_test") < output.index(expected_body)


def test_inbox_add_shows_the_new_id_and_preserves_json(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	note = {
		"id": "ibx_test",
		"project_id": "prj_test",
		"text": "Remember this",
		"created_at": "2026-09-29T12:00:00+00:00",
	}

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def inbox_add(self, text):
			assert text == "Remember this"
			return note

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	arguments = ["inbox", "add", "Remember", "this", "--database", str(tmp_path / "db")]

	assert cli.main(arguments) == 0
	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Added inbox note" in output
	assert "ibx_test" in output

	assert cli.main([*arguments, "--json"]) == 0
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": note}


def test_inbox_dismiss_shows_the_id_and_preserves_json(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	note_id = "inb_" + "n" * 22

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def inbox_dismiss(self, identifier):
			assert identifier == note_id
			return {"id": note_id}

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	arguments = ["inbox", "dismiss", note_id, "--database", str(tmp_path / "db")]

	assert cli.main(arguments) == 0
	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Dismissed inbox note" in output
	assert note_id in output

	assert cli.main([*arguments, "--json"]) == 0
	assert json.loads(capsys.readouterr().out) == {
		"ok": True,
		"data": {"id": note_id},
	}


def test_inbox_dismiss_returns_not_found_for_an_unknown_id(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	note_id = "inb_" + "n" * 22

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def inbox_dismiss(self, identifier):
			assert identifier == note_id
			raise NotFoundError("inbox note was not found", {"id": identifier})

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)

	assert (
		cli.main(
			["inbox", "dismiss", note_id, "--database", str(tmp_path / "db"), "--json"]
		)
		== 1
	)
	assert json.loads(capsys.readouterr().out)["error"] == {
		"code": "not-found",
		"message": "inbox note was not found",
		"details": {"id": note_id},
	}


@pytest.mark.parametrize("bare", [False, True])
def test_inbox_list_shows_notes_and_pagination(
	tmp_path: Path, monkeypatch, capsys, bare: bool
) -> None:
	data = {
		"items": [
			{
				"id": "ibx_first",
				"text": "First note",
				"created_at": "2026-09-29T12:00:00+00:00",
			},
			{
				"id": "ibx_second",
				"text": "Second note",
				"created_at": "2026-09-29T12:01:00+00:00",
			},
		],
		"limit": 2,
		"offset": 3,
		"has_more": True,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def inbox_list(self, limit, offset):
			assert (limit, offset) == (2, 3)
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)
	arguments = [
		"inbox",
		*([] if bare else ["list"]),
		"--limit",
		"2",
		"--offset",
		"3",
		"--database",
		str(tmp_path / "db"),
	]

	assert cli.main(arguments) == 0
	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert output.index("First note") < output.index("Second note")
	assert "ibx_first · 2026-09-29T12:00:00+00:00" in output
	assert "ibx_second · 2026-09-29T12:01:00+00:00" in output
	assert "More results: use --offset 5." in output

	assert cli.main([*arguments, "--json"]) == 0
	assert json.loads(capsys.readouterr().out) == {"ok": True, "data": data}


def test_empty_inbox_list_shows_an_empty_state(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def inbox_list(self, limit, offset):
			assert (limit, offset) == (50, 0)
			return {"items": [], "limit": limit, "offset": offset, "has_more": False}

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["inbox", "--database", str(tmp_path / "db")]) == 0
	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "No inbox notes." in output


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


def test_json_chunk_list_includes_task_and_pagination(
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

		def chunk_list(self, task_id, limit, offset, **kwargs):
			assert kwargs == {"collapse_done_chunks": False, "show_all": False}
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
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	assert json.loads(capsys.readouterr().out) == {
		"ok": True,
		"data": {
			**data,
			"task": {"id": "tsk_test", "title": "Progress task"},
		},
	}


@pytest.mark.parametrize("task_reference", ["tsk_test", "progress-task"])
@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_chunk_list_accepts_a_positional_task(
	tmp_path: Path, monkeypatch, capsys, task_reference: str, json_mode: bool
) -> None:
	calls = []

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def chunk_list(self, task_id, limit, offset, **kwargs):
			calls.append(("chunk_list", task_id))
			return {"items": [], "limit": 50, "offset": 0, "has_more": False}

		def task_get(self, task_id):
			calls.append(("task_get", task_id))
			return {"id": "tsk_test", "title": "Progress task"}

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)
	arguments = ["chunk", "list", task_reference, "--database", str(tmp_path / "db")]
	if json_mode:
		arguments.append("--json")

	assert cli.main(arguments) == 0
	output = capsys.readouterr()
	assert calls == [
		("chunk_list", task_reference),
		("task_get", task_reference),
	]
	if json_mode:
		assert json.loads(output.out)["data"]["task"]["id"] == "tsk_test"
	else:
		assert "Progress task · tsk_test" in output.out


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_chunk_list_rejects_both_task_forms(
	tmp_path: Path, capsys, json_mode: bool
) -> None:
	arguments = [
		"chunk",
		"list",
		"first",
		"--task",
		"second",
		"--database",
		str(tmp_path / "db"),
	]
	if json_mode:
		arguments.append("--json")

	assert cli.main(arguments) == 2
	output = capsys.readouterr()
	if json_mode:
		assert json.loads(output.out)["error"]["code"] == "usage"
	else:
		assert "give the task either as an argument or with --task" in output.err


@pytest.mark.parametrize("task_reference", ["tsk_test", "progress-task"])
@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_chunk_get_accepts_a_task_and_position(
	tmp_path: Path, monkeypatch, capsys, task_reference: str, json_mode: bool
) -> None:
	calls = []

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def chunk_get_by_position(self, task_id, position):
			calls.append((task_id, position))
			return {"id": "chk_test", "task_id": "tsk_test", "position": 2}

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)
	arguments = [
		"chunk",
		"get",
		task_reference,
		"position-2",
		"--database",
		str(tmp_path / "db"),
	]
	if json_mode:
		arguments.append("--json")

	assert cli.main(arguments) == 0
	output = capsys.readouterr()
	assert calls == [(task_reference, 2)]
	if json_mode:
		assert json.loads(output.out)["data"]["id"] == "chk_test"
	else:
		assert "chk_test" in output.out


@pytest.mark.parametrize("position_reference", ["position-0", "position-x", "2"])
def test_chunk_get_rejects_an_invalid_position(
	tmp_path: Path, capsys, position_reference: str
) -> None:
	assert (
		cli.main(
			[
				"chunk",
				"get",
				"first",
				position_reference,
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 2
	)
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_chunk_get_reports_a_missing_task_position(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def chunk_get_by_position(self, task_id, position):
			raise NotFoundError(
				f"chunk at position {position} was not found in task {task_id}",
				{"task_id": task_id, "position": position},
			)

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"chunk",
				"get",
				"first",
				"position-2",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 1
	)
	assert json.loads(capsys.readouterr().out)["error"] == {
		"code": "not-found",
		"message": "chunk at position 2 was not found in task first",
		"details": {"task_id": "first", "position": 2},
	}


@pytest.mark.parametrize("page_flag", ["--limit", "--offset"])
def test_chunk_list_all_rejects_explicit_paging(
	tmp_path: Path, capsys, page_flag: str
) -> None:
	assert (
		cli.main(
			[
				"chunk",
				"list",
				"--all",
				page_flag,
				"1",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 2
	)
	assert "--all cannot be used with --limit or --offset" in capsys.readouterr().err


def test_json_chunk_list_all_removes_page_limit(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def chunk_list(self, task_id, limit, offset, *, collapse_done_chunks, show_all):
			assert (task_id, limit, offset) == ("tsk_test", 50, 0)
			assert collapse_done_chunks is False
			assert show_all is True
			return {"items": [], "limit": None, "offset": 0, "has_more": False}

		def task_get(self, task_id):
			return {"id": task_id, "title": "Progress task"}

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert (
		cli.main(
			[
				"chunk",
				"list",
				"--task",
				"tsk_test",
				"--all",
				"--json",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)
	assert json.loads(capsys.readouterr().out)["data"] == {
		"items": [],
		"limit": None,
		"offset": 0,
		"has_more": False,
		"task": {"id": "tsk_test", "title": "Progress task"},
	}


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
		contract=["Review context contract."],
		release_id=later_release["id"],
		position=1,
	)
	active_task = writer.task_add(
		"progress-cli-read-parity",
		"Progress CLI read parity",
		overview="Keep read commands aligned.",
		contract=["Keep read ordering aligned."],
		release_id=active_release["id"],
		position=1,
	)
	unassigned_task = writer.task_add(
		"unassigned-task",
		"Unassigned task",
		overview="An unassigned task.",
		contract=["Unassigned tasks use the final queue bucket."],
		position=1,
	)

	assert cli.main(["task", "list", "--json", "--database", str(database_path)]) == 0
	json_response = json.loads(capsys.readouterr().out)
	json_ids = [item["id"] for item in json_response["data"]["items"]]

	assert json_ids == [active_task["id"], later_task["id"], unassigned_task["id"]]

	assert cli.main(["task", "list", "--database", str(database_path)]) == 0
	human_output = capsys.readouterr().out

	assert human_output.index("Project review context") < human_output.index(
		"Progress CLI read parity"
	)
	assert human_output.index("Unassigned task") < human_output.index(
		"Project review context"
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
					"position": 1,
					"title": "Done output",
					"status": "done",
					"description": "Done descriptions are hidden.",
				},
				{
					"id": "chk_done_other",
					"position": 2,
					"title": "Other done output",
					"status": "done",
				},
				{
					"id": long_identifier,
					"position": 3,
					"title": "Active output",
					"status": "active",
					"description": active_description,
				},
				{
					"id": "chk_active_other",
					"position": 4,
					"title": "Other active output",
					"status": "active",
				},
				{
					"id": "chk_pending",
					"position": 5,
					"title": "Pending output",
					"status": "pending",
					"description": pending_description,
				},
				{
					"id": "chk_pending_other",
					"position": 6,
					"title": "Other pending output",
					"status": "pending",
				},
			],
			"has_more": False,
			"status_counts": {"done": 2, "active": 2, "pending": 2},
			"next_chunk": "Active output",
		},
	)

	assert output == (
		"Chunks\n\n"
		"6. Other pending output\nskipped:pending · chk_pending_other\n\n"
		"5. Pending output\nskipped:pending · chk_pending\n\n"
		f"{pending_description.splitlines()[0]}\n\n"
		"4. Other active output\ninfo:active · chk_active_other\n\n"
		f"3. Active output\ninfo:active · {long_identifier}\n\n"
		f"{active_description.splitlines()[0]}\n\n"
		"2. Other done output\nsuccess:done · chk_done_other\n\n"
		"1. Done output\nsuccess:done · chk_done\n\n"
		"6 chunks: 2 active, 2 pending, 2 done. Next: Active output."
	)
	assert "Done descriptions are hidden." not in output
	assert "This line is hidden." not in output
	assert "This line is hidden too." not in output
	assert any(long_identifier in line for line in output.splitlines())
	assert "Description" not in output
	assert "1. Done output\nsuccess:done · chk_done" in output
	assert status_calls == [
		("skipped", "pending", ""),
		("skipped", "pending", ""),
		("info", "active", ""),
		("info", "active", ""),
		("success", "done", ""),
		("success", "done", ""),
	]
	assert ("1. Done output", "text", "normal") in span_calls
	assert ("3. Active output", "text", "normal") in span_calls
	assert ("5. Pending output", "text", "normal") in span_calls
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
				{
					"id": "chk_active",
					"position": 1,
					"title": "Active output",
					"status": "active",
				},
				{
					"id": "chk_skipped",
					"position": 2,
					"title": "Skipped output",
					"status": "skipped",
				},
				{
					"id": "chk_pending",
					"position": 3,
					"title": "Pending output",
					"status": "pending",
				},
			],
			"has_more": False,
			"status_counts": {"active": 1, "skipped": 1, "pending": 1},
			"next_chunk": "Active output",
		},
	)

	assert output == (
		"Chunks\n\n"
		"3. Pending output\nskipped:pending · chk_pending\n\n"
		"2. Skipped output\nskipped:pending · chk_skipped\n\n"
		"1. Active output\ninfo:active · chk_active\n\n"
		"3 chunks: 1 active, 1 pending, 1 skipped. Next: Active output."
	)
	assert status_calls == [
		("skipped", "pending", ""),
		("skipped", "pending", ""),
		("info", "active", ""),
	]


def test_chunk_list_shows_done_count_above_reversed_rows(monkeypatch) -> None:
	monkeypatch.setattr(
		render_module, "render_span", lambda value, *args, **kwargs: value
	)
	monkeypatch.setattr(
		render_module,
		"render_status",
		lambda result_type, label="", detail="": label,
	)

	output = render_module._render_chunk_list(
		{
			"items": [
				{
					"id": "chk_first",
					"position": 1,
					"title": "First",
					"status": "active",
				},
				{
					"id": "chk_second",
					"position": 2,
					"title": "Second",
					"status": "pending",
				},
			],
			"done_count": 3,
			"status_counts": {"active": 1, "pending": 1, "done": 3},
			"next_chunk": "First",
		}
	)

	assert output.index("3 done") < output.index("2. Second") < output.index("1. First")
	assert "5 chunks: 1 active, 1 pending, 3 done. Next: First." in output


def test_chunk_list_with_only_done_count_omits_empty_message(monkeypatch) -> None:
	monkeypatch.setattr(
		render_module, "render_span", lambda value, *args, **kwargs: value
	)

	output = render_module._render_chunk_list(
		{"items": [], "done_count": 2, "status_counts": {"done": 2}}
	)

	assert "2 done" in output
	assert "No chunks." not in output
	assert output.endswith("2 chunks: 2 done. Next: none.")


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
				{"id": "chk_test", "position": 10, "title": title, "status": "active"},
			],
			"has_more": False,
			"status_counts": {"active": 1},
			"next_chunk": title,
		},
	)

	lines = output.splitlines()
	assert lines == [
		"Chunks",
		"",
		"10. A chunk title that wraps across the terminal width before its status",
		"    line",
		"active · chk_test",
		"",
		f"1 chunk: 1 active. Next: {title}.",
	]
	assert all(len(line) <= 72 for line in lines[2:4])


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
					"position": 1,
					"title": "Render output",
					"status": "ready",
					"description": description,
				}
			],
			"has_more": False,
			"status_counts": {"pending": 1},
			"next_chunk": "Render output",
		},
	)

	assert output == (
		"Chunks\n\n1. Render output\nskipped · chk_test\n\n"
		"1 chunk: 1 pending. Next: Render output."
	)
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

	assert output == (
		"Chunks\n\nNo chunks.\n\nMore results: use --offset 1.\n\n0 chunks. Next: none."
	)


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
			"items": [
				{
					"id": "tsk_test",
					"title": "Task",
					"status": "ready",
					"queue_number": 1,
				}
			],
			"has_more": False,
			"release_order": [{"id": None, "title": None}],
			"done_counts": {},
			"releases_with_unfinished": {None},
			"status_counts": {"ready": 1},
			"next_task": "Task",
		}
	)

	assert output == (
		"<Unassigned>\n\n"
		"table\n\n"
		"View a task with <progress task get TASK_ID>; reorder with "
		"<progress task move TASK_ID --before/--after TASK_ID>.\n\n"
		"1 task: 1 ready. Next: Task."
	)
	assert ("progress task get TASK_ID", "info", "bold") in spans
	assert (
		"progress task move TASK_ID --before/--after TASK_ID",
		"info",
		"bold",
	) in spans
	assert ("Unassigned", "info", "normal") in spans
	assert tables == [
		(
			[
				{"key": "number", "label": "#"},
				{"key": "status", "label": "Status"},
				{"key": "title", "label": "Title"},
				{"key": "id", "label": "ID"},
			],
			[
				{
					"number": "1",
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
		render_module._render_task_list(
			{
				"items": [],
				"has_more": False,
				"release_order": [],
				"done_counts": {},
				"releases_with_unfinished": set(),
				"status_counts": {},
				"next_task": None,
			}
		)
		== "No tasks.\n\n0 tasks. Next: none."
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
		"verification": "Run the task tests.",
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
	assert "Verification" in plain_output
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
	assert plain_output.index("Overview") < plain_output.index("Chunks")
	assert plain_output.index("Chunks") < plain_output.index("Split rationale")
	assert plain_output.index("Split rationale") < plain_output.index("Contract")
	assert plain_output.index("Contract") < plain_output.index("Files")
	assert plain_output.index("Files") < plain_output.index("Verification")
	assert plain_output.index("Verification") < plain_output.index("Project ID")


def test_release_get_uses_one_readable_release_view() -> None:
	data = {
		"id": "rel_release_view",
		"project_id": "prj_release_view",
		"slug": "hidden-release-slug",
		"title": "Readable release",
		"overview": (
			"The release overview.\n\nOut of scope:\n- First item.\n- Second item."
		),
		"status": "in-progress",
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
	assert "Overview" in plain_output
	assert (
		"The release overview.\n\nOut of scope:\n- First item.\n- Second item."
		in plain_output
	)
	assert "Purpose" not in plain_output
	assert "Risks" not in plain_output
	assert plain_output.count("Notes") == 1
	assert "Release notes" not in plain_output
	assert "release rel_release_view" not in plain_output
	assert "The full release note.\nThe second note line." in plain_output
	assert "hidden-release-slug" not in plain_output
	assert "position" not in plain_output.lower()
	assert plain_output.index("Readable release") < plain_output.index("Status")
	assert plain_output.index("Status") < plain_output.index("Overview")
	assert plain_output.index("Overview") < plain_output.index("Notes")


def test_release_get_omits_empty_optional_sections() -> None:
	output = render_module.render(
		"release get",
		{
			"id": "rel_empty_view",
			"title": "Minimal release",
			"status": "planned",
			"overview": "Overview only.",
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


def test_json_release_get_includes_overview_and_notes(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"id": "rel_test",
		"title": "Release",
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

		def next(self, *, task_id=None, include_position_totals=False):
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

		def next(self, *, task_id=None, include_position_totals=False):
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

		def next(self, *, task_id=None, include_position_totals=False):
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

		def next(self, *, task_id=None, include_position_totals=False):
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
		("waiting", "info"),
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
		{"id": "tsk_test", "title": "Task", "status": status}, 1
	)

	assert _status_result_type(status) == result_type
	assert row["status"] == f"{result_type}:{status}".ljust(16)
	assert row["title"] == "Task"
	assert row["id"] == "tsk_test"


def test_json_task_list_does_not_request_release_titles(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"items": [
			{"id": "tsk_test", "title": "Read surface", "status": "ready"},
			{"id": "tsk_done", "title": "Finished task", "status": "done"},
		],
		"limit": 50,
		"offset": 0,
		"has_more": False,
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def task_list(
			self,
			status,
			limit,
			offset,
			*,
			include_release_titles,
			include_queue_numbers,
			collapse_done_tasks,
			show_all,
		):
			assert status is None
			assert limit == 50
			assert offset == 0
			assert include_release_titles is False
			assert include_queue_numbers is False
			assert collapse_done_tasks is False
			assert show_all is False
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
		'"status":"ready"},{"id":"tsk_done","title":"Finished task",'
		'"status":"done"}],"limit":50,"offset":0,"has_more":false}}\n'
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


@pytest.mark.parametrize("action", ["edit", "update"])
def test_task_edit_dispatches_values_and_optional_clear_flags(
	tmp_path: Path, monkeypatch, capsys, action: str
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
				action,
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
		"contract": None,
		"files": None,
		"split_rationale": None,
		"verification": None,
		"clear_files": False,
		"clear_split_rationale": False,
		"clear_verification": False,
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
		"contract": ["Updated first", "Updated second"],
		"files": ["src/updated.py"],
		"split_rationale": "The chunks have separate review questions",
		"verification": None,
		"clear_files": False,
		"clear_split_rationale": False,
		"clear_verification": False,
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


@pytest.mark.parametrize("action", ["edit", "update"])
def test_chunk_edit_dispatches_description(
	tmp_path: Path,
	monkeypatch,
	capsys,
	action: str,
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
				action,
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
		"review_question": None,
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
				"--review-question",
				"Does it work?",
				"--database",
				str(tmp_path / "db"),
			]
		)
		== 0
	)

	assert "Next: progress task start tsk_test" in capsys.readouterr().out


@pytest.mark.parametrize("secondary", [False, True])
def test_human_task_start_hint_selects_started_task(
	tmp_path: Path, monkeypatch, capsys, secondary: bool
) -> None:
	task_id = "tsk_test"
	data = {"id": task_id, "title": "Selected task", "status": "in-progress"}
	expected_secondary = secondary

	class _WriteStore:
		def __init__(self, database) -> None:
			pass

		def task_start(self, selected_id: str, *, secondary: bool):
			assert selected_id == task_id
			assert secondary is expected_secondary
			return data

	monkeypatch.setattr(cli, "WriteStore", _WriteStore)
	arguments = ["task", "start", task_id, "--database", str(tmp_path / "db")]
	if secondary:
		arguments.append("--secondary")

	assert cli.main(arguments) == 0

	assert f"Next: progress next --task {task_id}" in capsys.readouterr().out


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
		"worktree_cleanup": {"status": "pending", "reason": "checkout is locked"},
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
	assert plain_lines[1].endswith(
		"Completed task Dependency | Worktree: pending (checkout is locked)"
	)
	# Inequality after stripping ANSI proves the line carried styling.
	assert plain_lines[1] != (
		"Completed task Dependency | Worktree: pending (checkout is locked)"
	)
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
	assert output.endswith("\n\nNext: progress next --task tsk_parent\n")
	assert len(plain_lines) == 5
	assert plain_lines[1].endswith("Completed chunk CLI output")
	# Inequality after stripping ANSI proves the line carried styling.
	assert plain_lines[1] != "Completed chunk CLI output"
	assert plain_lines[2] == "Task ID: tsk_parent"
	assert plain_lines[4] == "Next: progress next --task tsk_parent"
	assert ("Task ID: tsk_parent", "muted", "normal") in render_spans
	assert ("Next: progress next --task tsk_parent", "muted", "normal") in next_spans
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


def test_worktree_commands_return_the_task_checkout_without_creating_on_get(
	tmp_path: Path, committed_repository: Path, monkeypatch, capsys
) -> None:
	repository = committed_repository
	monkeypatch.chdir(repository)
	database = Database(tmp_path / "progress.db")
	project, _ = ProjectStore(database).init("agents", "Agent configuration")
	task = WriteStore(database).task_add(
		"first-task", "First task", "Work in isolation", ["Finish the work"]
	)
	arguments = ["--database", str(database.path), "--json"]

	assert cli.main(["worktree", "get", task["id"], *arguments]) == 1
	missing = json.loads(capsys.readouterr().out)
	with database.connection() as connection:
		assert (
			connection.execute("SELECT COUNT(*) FROM task_worktrees").fetchone()[0] == 0
		)
		assert connection.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 0

	assert missing["error"]["code"] == "not-found"
	assert cli.main(["worktree", "ensure", task["id"], *arguments]) == 0
	created = json.loads(capsys.readouterr().out)["data"]
	assert created["project_id"] == project.id
	assert created["created"] is True
	assert Path(created["path"]).is_dir()
	with database.connection() as connection:
		checkouts = connection.execute("SELECT path FROM checkouts").fetchall()
		assert [row["path"] for row in checkouts] == [str(repository)]

	assert cli.main(["worktree", "get", task["id"], *arguments]) == 0
	got = json.loads(capsys.readouterr().out)["data"]
	assert got == {key: value for key, value in created.items() if key != "created"}
	with database.connection() as connection:
		assert connection.execute("SELECT COUNT(*) FROM checkouts").fetchone()[0] == 1
	assert (
		cli.main(["worktree", "ensure", task["id"], "--database", str(database.path)])
		== 0
	)
	assert created["path"] in capsys.readouterr().out


def test_worktree_cleanup_command_reports_pending_and_removed(
	tmp_path: Path, committed_repository: Path, monkeypatch, capsys
) -> None:
	repository = committed_repository
	monkeypatch.chdir(repository)
	database = Database(tmp_path / "progress.db")
	ProjectStore(database).init("agents", "Agent configuration")
	task = WriteStore(database).task_add(
		"cleanup-task", "Cleanup task", "Finish the work", ["Complete the task"]
	)
	arguments = ["--database", str(database.path), "--json"]

	assert cli.main(["worktree", "cleanup", task["id"], *arguments]) == 1
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid-transition"

	assert cli.main(["worktree", "ensure", task["id"], *arguments]) == 0
	checkout = Path(json.loads(capsys.readouterr().out)["data"]["path"])
	(checkout / "tracked.txt").write_text("unfinished work\n")

	assert cli.main(["task", "complete", task["id"], *arguments]) == 0
	completed = json.loads(capsys.readouterr().out)["data"]
	assert completed["status"] == "done"
	assert completed["worktree_cleanup"]["status"] == "pending"

	assert cli.main(["worktree", "cleanup", task["id"], *arguments]) == 0
	assert json.loads(capsys.readouterr().out)["data"]["status"] == "pending"

	(checkout / "tracked.txt").write_text("committed content\n")

	assert (
		cli.main(["worktree", "cleanup", task["id"], "--database", str(database.path)])
		== 0
	)
	assert "Worktree: removed" in capsys.readouterr().out
	assert not checkout.exists()

	assert cli.main(["worktree", "cleanup", task["id"], *arguments]) == 0
	assert json.loads(capsys.readouterr().out)["data"] == {"status": "none"}


def test_dispatch_records_project_commands_once_and_skips_help_version_and_command_list(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
	monkeypatch.chdir(tmp_path)
	database = tmp_path / "progress.db"
	arguments = ["--database", str(database), "--json"]

	assert (
		cli.main(
			["project", "init", "--slug", "agents", "--name", "Agents", *arguments]
		)
		== 0
	)
	capsys.readouterr()
	with Database(database).connection() as connection:
		row = connection.execute("SELECT path, project_id FROM checkouts").fetchone()
		assert row is not None
		assert row["path"] == str(tmp_path)
		assert row["project_id"] == ProjectStore(Database(database)).current().id

	with pytest.MonkeyPatch.context() as patch:
		calls = []
		patch.setattr(ProjectStore, "record_checkout", lambda self: calls.append(True))
		assert cli.main(["project", "current", *arguments]) == 0
		assert cli.main(["task", "get", "tsk_" + "a" * 22, *arguments]) == 1
		assert calls == [True, True]
		capsys.readouterr()

		assert cli.main(["--version"]) == 0
		assert cli.main(["commands", *arguments]) == 0
		assert cli.main(["project"]) == 0
		assert calls == [True, True]


def test_checkout_recording_failure_does_not_change_command_result(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
	monkeypatch.chdir(tmp_path)
	database = tmp_path / "progress.db"
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
				str(database),
				"--json",
			]
		)
		== 0
	)
	capsys.readouterr()

	def fail_recording(self) -> None:
		raise RuntimeError("checkout write failed")

	monkeypatch.setattr(ProjectStore, "record_checkout", fail_recording)

	assert cli.main(["project", "current", "--database", str(database), "--json"]) == 0
	assert json.loads(capsys.readouterr().out)["data"]["slug"] == "agents"


def test_summary_runs_outside_a_repository_without_recording_checkout(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	database = tmp_path / "progress.db"
	with Database(database).transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			("prj_" + "a" * 22, "agents", "Agents", "2026-01-01T00:00:00+00:00"),
		)
	monkeypatch.chdir(tmp_path)
	calls = []
	monkeypatch.setattr(
		ProjectStore, "record_checkout", lambda self: calls.append(True)
	)

	assert cli.main(["summary", "--database", str(database), "--json"]) == 0
	response = json.loads(capsys.readouterr().out)
	assert response["ok"] is True
	assert response["data"][0]["project"]["name"] == "Agents"
	assert response["data"][0]["checkouts"] == []
	assert calls == []

	assert cli.main(["summary", "--database", str(database)]) == 0
	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert "Agents\n\nNo current tasks\n" in output
	assert "Checkouts:" not in output
	assert "Next action:" not in output
	assert "Hint:" not in output
	assert calls == []


def test_summary_marks_missing_checkout_paths_as_stale(tmp_path: Path, capsys) -> None:
	database = tmp_path / "progress.db"
	live_checkout = tmp_path / "agents"
	live_checkout.mkdir()
	stale_checkout = tmp_path / "missing"
	with Database(database).transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			("prj_" + "a" * 22, "agents", "Agents", "2026-01-01T00:00:00+00:00"),
		)
		for checkout in (live_checkout, stale_checkout):
			connection.execute(
				"INSERT INTO checkouts (path, project_id, last_seen_at) VALUES (?, ?, ?)",
				(str(checkout), "prj_" + "a" * 22, "2026-01-02T12:00:00+00:00"),
			)

	assert cli.main(["summary", "--database", str(database), "--json"]) == 0
	response = json.loads(capsys.readouterr().out)
	assert {
		checkout["path"]: checkout["stale"]
		for checkout in response["data"][0]["checkouts"]
	} == {str(live_checkout): False, str(stale_checkout): True}

	assert cli.main(["summary", "--database", str(database)]) == 0
	output = render_module._ANSI_ESCAPE_PATTERN.sub("", capsys.readouterr().out)
	assert f"Agents · {live_checkout} · {stale_checkout} (stale)\n" in output
	assert "last seen" not in output
	assert "Checkout:" not in output


def test_checkout_detach_runs_outside_a_repository_without_recording(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	database = tmp_path / "progress.db"
	checkout = tmp_path / "checkout"
	stale_checkout = tmp_path / "missing"
	checkout.mkdir()
	with Database(database).transaction() as connection:
		connection.execute(
			"INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
			("prj_" + "a" * 22, "agents", "Agents", "2026-01-01T00:00:00+00:00"),
		)
		connection.execute(
			"INSERT INTO checkouts (path, project_id, last_seen_at) VALUES (?, ?, ?)",
			(str(checkout), "prj_" + "a" * 22, "2026-01-02T00:00:00+00:00"),
		)
		connection.execute(
			"INSERT INTO checkouts (path, project_id, last_seen_at) VALUES (?, ?, ?)",
			(str(stale_checkout), "prj_" + "a" * 22, "2026-01-02T00:00:00+00:00"),
		)
	monkeypatch.chdir(tmp_path)
	calls = []
	monkeypatch.setattr(
		ProjectStore, "record_checkout", lambda self: calls.append(True)
	)

	assert (
		cli.main(
			["checkout", "detach", "checkout", "--database", str(database), "--json"]
		)
		== 0
	)
	assert json.loads(capsys.readouterr().out)["data"] == [str(checkout)]
	assert calls == []
	assert cli.main(["checkout", "detach", "--stale", "--database", str(database)]) == 0
	assert f"Detached checkout: {stale_checkout}" in capsys.readouterr().out
	assert cli.main(["checkout", "detach", "--stale", "--database", str(database)]) == 0
	assert capsys.readouterr().out.strip() == "No checkouts detached"
	assert calls == []


def test_checkout_detach_reports_unrecorded_paths_and_requires_a_selection(
	tmp_path: Path, capsys
) -> None:
	database = tmp_path / "progress.db"
	missing = tmp_path / "missing"

	assert (
		cli.main(["checkout", "detach", str(missing), "--database", str(database)]) == 1
	)
	human_error = capsys.readouterr()
	assert human_error.out == ""
	assert f"checkout {missing} was not found" in _stderr_error_message(human_error.err)
	assert "usage:" not in human_error.err

	assert (
		cli.main(
			["checkout", "detach", str(missing), "--database", str(database), "--json"]
		)
		== 1
	)
	response = json.loads(capsys.readouterr().out)
	assert response["error"] == {
		"code": "not-found",
		"message": f"checkout {missing} was not found",
		"details": {"path": str(missing)},
	}
	assert cli.main(["checkout", "detach", "--database", str(database), "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_summary_checkout_path_shows_home_as_tilde_and_keeps_path_without_home(
	monkeypatch,
) -> None:
	monkeypatch.setattr(Path, "home", lambda: Path("/work"))
	assert render_module._summary_checkout_path("/work") == "~"

	def raise_missing_home() -> Path:
		"""Fail the way Path.home() does when HOME cannot be found."""
		raise RuntimeError("Could not determine home directory.")

	monkeypatch.setattr(Path, "home", raise_missing_home)
	assert render_module._summary_checkout_path("/work/active") == "/work/active"


def test_human_summary_shows_active_ready_chunkless_and_idle_projects(
	monkeypatch,
) -> None:
	monkeypatch.setattr(Path, "home", lambda: Path("/work"))
	output = render_module._ANSI_ESCAPE_PATTERN.sub(
		"",
		render_module.render(
			"summary",
			[
				{
					"project": {"name": "Idle first"},
					"checkouts": [],
					"task": None,
				},
				{
					"project": {"name": "Active"},
					"checkouts": [
						{
							"path": "/work/active",
							"last_seen_at": "2026-01-02T13:00:30.123456+01:00",
							"stale": False,
						},
						{"path": "/work/second", "stale": True},
						{"path": "/workspace/other", "stale": False},
					],
					"task": {"title": "Build summary", "status": "in-progress"},
					"chunk": {"title": "Lay out blocks", "status": "active"},
					"chunk_rank": 2,
					"chunk_total": 3,
					"commit_plan": {"done": 1, "total": 3},
					"other_task_counts": {"ready": 2, "blocked": 1, "done": 4},
					"release": {"title": "Progress tools"},
					"next_action": "Finish the summary after checking every project",
					"hint_command": "progress chunk complete chk_example",
				},
				{
					"project": {"name": "Ready"},
					"checkouts": [],
					"task": {"title": "Plan follow-up", "status": "ready"},
					"chunk": None,
					"commit_plan": {"done": 0, "total": 9},
					"other_task_counts": {},
					"release": None,
				},
				{
					"project": {"name": "Chunkless"},
					"checkouts": [],
					"task": {"title": "Review notes", "status": "waiting"},
					"chunk": None,
					"commit_plan": None,
					"other_task_counts": {},
					"release": None,
				},
				{
					"project": {"name": "Idle last"},
					"checkouts": [],
					"task": None,
				},
			],
		),
	)

	assert output == (
		"Active · ~/active · ~/second (stale) · /workspace/other\n"
		"\n"
		"Build summary · in progress\n"
		"Lay out blocks · active · (2/3)\n"
		"Release: Progress tools\n"
		"\n"
		"Other tasks: 2 ready · 1 blocked\n"
		"\n"
		"----------------------------------------\n\n"
		"Ready\n"
		"\n"
		"Plan follow-up · ready · 0/9 chunks\n"
		"\n"
		"----------------------------------------\n\n"
		"Chunkless\n"
		"\n"
		"Review notes · waiting\n"
		"\n"
		"----------------------------------------\n\n"
		"Idle first\n"
		"\n"
		"No current tasks\n"
		"\n"
		"----------------------------------------\n\n"
		"Idle last\n"
		"\n"
		"No current tasks\n"
		"\n"
		"----------------------------------------"
	)


def test_human_summary_keeps_long_release_title_on_one_line(monkeypatch) -> None:
	monkeypatch.setenv("NO_COLOR", "1")
	release_title = ("A long release title " * 7).rstrip()
	output = render_module._ANSI_ESCAPE_PATTERN.sub(
		"",
		render_module.render(
			"summary",
			[
				{
					"project": {"name": "Agents"},
					"checkouts": [],
					"task": {"title": "Build summary", "status": "ready"},
					"chunk": None,
					"commit_plan": None,
					"other_task_counts": {},
					"release": {"title": release_title},
				}
			],
		),
	)

	assert len(release_title) > 120
	assert f"Release: {release_title}" in output.splitlines()
