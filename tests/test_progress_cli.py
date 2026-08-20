import json
from pathlib import Path

import pytest
import agents_progress.render as render_module
import agents_progress.style as style_module
from agents_progress import cli
from agents_progress.errors import AlreadyExistsError, DuplicateDependencyError
from agents_progress.render import _status_result_type


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
			"{add,move,dependency,remove,rename,edit,start,complete,block,unblock,get,list}",
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
	assert output.err == (
		f"Error: the following arguments are required: {expected_choices}\n"
	)


def test_top_level_help_uses_a_short_usage_placeholder() -> None:
	help_text = cli.build_parser().format_help()
	usage_line = help_text.splitlines()[0]

	assert "COMMAND" in usage_line
	assert "{next,current,doctor" not in usage_line
	assert "\n  COMMAND\n" in help_text
	assert "    task           read task records" in help_text


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
			"title": "Read surface",
			"status": "in-progress",
			"overview": "Read the current task.",
			"status_reason": "Waiting for a decision",
		},
		"chunk": {
			"id": "chk_test",
			"title": "CLI output",
			"description": "Render readable output.",
			"status": "active",
		},
		"dependency_ids": ["tsk_dependency"],
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
	assert output.out.startswith("\n")
	assert output.out.endswith("\n\n")
	assert "Project" in output.out and "Agents" in output.out
	assert "Task" in output.out and "Read surface" in output.out
	assert "Task status" in output.out and "in-progress" in output.out
	assert "Blocking reason" in output.out and "Waiting for a decision" in output.out
	assert "Dependency IDs" in output.out and "tsk_dependency" in output.out
	assert "Next action: progress chunk complete chk_test" in output.out
	assert "i Hint: progress chunk complete chk_test" not in output.out


def test_row_group_requests_72_column_wrap(monkeypatch) -> None:
	rows = [{"label": "Overview", "value": "A long task overview."}]
	calls: list[tuple[str, dict[str, object]]] = []

	def fake_render(renderer: str, data: dict[str, object]) -> str:
		calls.append((renderer, data))
		return "rendered"

	monkeypatch.setattr(style_module, "render_generic", fake_render)

	assert style_module.row_group(rows) == "rendered"

	assert calls == [("row-group", {"rows": rows, "wrapWidth": 72})]


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


def test_chunk_list_renders_descriptions_in_wrapped_rows(monkeypatch) -> None:
	descriptions: list[list[dict[str, str]]] = []

	monkeypatch.setattr(render_module, "render_span", lambda value, *args: value)
	monkeypatch.setattr(
		render_module,
		"render_row",
		lambda label, value, result="": f"{label}: {value}",
	)

	def fake_row_group(rows: list[dict[str, str]]) -> str:
		descriptions.append(rows)
		return f"Description: {rows[0]['value']}"

	monkeypatch.setattr(render_module, "render_row_group", fake_row_group)

	description = "A long description explains the full scope of this chunk."
	output = render_module._render_list(
		"chunk list",
		{
			"items": [
				{
					"id": "chk_test",
					"title": "Render output",
					"status": "ready",
					"description": description,
				},
				{
					"id": "chk_other",
					"title": "Other output",
					"status": "pending",
					"description": "Another chunk description.",
				},
			],
			"has_more": False,
		},
	)

	assert output == (
		"Chunks\n\n"
		"Render output: ready (chk_test)\n"
		f"Description: {description}\n\n"
		"Other output: pending (chk_other)\n"
		"Description: Another chunk description."
	)
	assert descriptions == [
		[{"label": "Description", "value": description}],
		[
			{
				"label": "Description",
				"value": "Another chunk description.",
			}
		],
	]


@pytest.mark.parametrize("description", [None, ""])
def test_chunk_list_omits_empty_descriptions(description, monkeypatch) -> None:
	monkeypatch.setattr(render_module, "render_span", lambda value, *args: value)
	monkeypatch.setattr(
		render_module,
		"render_row",
		lambda label, value, result="": f"{label}: {value}",
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

	assert "Render output: ready (chk_test)" in output
	assert "Description" not in output


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
				"overview": "A task overview.",
				"purpose": "A task purpose.",
				"contract": "A task contract.",
				"status": "ready",
			},
			[
				{"label": "Overview", "value": "A task overview."},
				{"label": "Purpose", "value": "A task purpose."},
				{"label": "Contract", "value": "A task contract."},
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

	def fake_row_group(rows: list[dict[str, str]]) -> str:
		groups.append(rows)
		return "Grouped planning fields"

	monkeypatch.setattr(render_module, "render_row_group", fake_row_group)

	output = render_module.render(command, data)

	assert "Grouped planning fields" in output
	assert groups == [expected_rows]


def test_human_next_renders_done_chunk_with_success_status(
	tmp_path: Path, monkeypatch, capsys
) -> None:
	data = {
		"project": {"id": "prj_test", "slug": "agents", "name": "Agents"},
		"task": {"id": "tsk_test", "title": "Read surface", "status": "ready"},
		"chunk": {
			"id": "chk_test",
			"title": "CLI output",
			"description": "Render readable output.",
			"status": "done",
		},
	}

	class _ReadStore:
		def __init__(self, database) -> None:
			pass

		def next(self):
			return data

	monkeypatch.setattr(cli, "ReadStore", _ReadStore)

	assert cli.main(["next", "--database", str(tmp_path / "db")]) == 0

	output = capsys.readouterr()

	assert "✓ Chunk status done" in output.out or "OK Chunk status done" in output.out


@pytest.mark.parametrize(
	("status", "result_type"),
	[
		("ready", "skipped"),
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
				"--clear-model-tier",
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
		"model_tier": None,
		"files": None,
		"acceptance_criteria": None,
		"verification": None,
		"risks": None,
		"clear_model_tier": True,
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


@pytest.mark.parametrize(
	("unblocked_tasks", "expected_output"),
	[
		(
			[{"id": "tsk_dependent", "slug": "dependent", "title": "Dependent"}],
			"- Dependent (tsk_dependent)",
		),
		([], "Unblocked tasks: none"),
	],
)
def test_human_task_complete_output_names_unblocked_tasks(
	tmp_path: Path, monkeypatch, capsys, unblocked_tasks, expected_output
) -> None:
	data = {
		"id": "tsk_test",
		"slug": "dependency",
		"title": "Dependency",
		"status": "done",
		"unblocked_tasks": unblocked_tasks,
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

	assert expected_output in output


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
