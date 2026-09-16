"""Tests for storing and listing named project commands."""

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest
from agent_run import schema
from agent_run.cli import main
from agent_run.commands import (
    Command,
    CommandError,
    CommandNotFoundError,
    add_command,
    edit_command,
    list_commands,
    remove_command,
    rename_command,
)
from agent_run.database import connect_database
from agent_run.repository import Repository


def _initialise_repository(path: Path) -> Path:
    """Create an empty temporary Git repository for a command test."""
    path.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)

    return path


def _repository(path: Path) -> Repository:
    """Return a repository identity suitable for direct command tests."""
    return Repository(root=path.resolve(), id="repository-id")


def test_add_command_stores_repository_relative_directory(tmp_path: Path) -> None:
    """Adding a command stores its argument array and relative directory."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    repository = _repository(root)

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        command = add_command(
            connection,
            repository,
            "format",
            ["ruff", "check"],
            "tools",
        )
        rows = connection.execute(
            "SELECT repository_id, name, argv, working_directory FROM commands"
        ).fetchall()
    finally:
        connection.close()

    assert command.name == "format"
    assert command.argv == ("ruff", "check")
    assert command.working_directory == "tools"
    assert rows == [("repository-id", "format", '["ruff", "check"]', "tools")]


def test_add_command_uses_dot_for_repository_root(tmp_path: Path) -> None:
    """Adding a command without a directory stores the repository root as `.`."""
    root = _initialise_repository(tmp_path / "repository")
    repository = _repository(root)

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        command = add_command(connection, repository, "test", ["pytest"])
    finally:
        connection.close()

    assert command.working_directory == "."


def test_list_commands_orders_by_name_and_repository_scope(tmp_path: Path) -> None:
    """Listing orders names and excludes commands from other repositories."""
    root = _initialise_repository(tmp_path / "repository")
    repository = _repository(root)
    other_repository = Repository(root=root, id="other-repository")

    connection = connect_database(tmp_path / "agent-run.db")
    try:
        add_command(connection, repository, "z-test", ["z"])
        add_command(connection, repository, "a-test", ["a"])
        add_command(connection, other_repository, "ignored", ["ignored"])
        commands = list_commands(connection, repository)
    finally:
        connection.close()

    assert [command.name for command in commands] == ["a-test", "z-test"]
    assert [list(command.argv) for command in commands] == [["a"], ["z"]]


def test_edit_command_changes_arguments_and_directory(tmp_path: Path) -> None:
    """Editing changes selected fields while keeping the creation timestamp."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    repository = _repository(root)
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        original = add_command(connection, repository, "test", ["pytest"])
        edited = edit_command(
            connection,
            repository,
            "test",
            ["pytest", "tests"],
            "tools",
        )
        row = connection.execute(
            "SELECT argv, working_directory, created_at FROM commands"
        ).fetchone()
    finally:
        connection.close()

    assert edited == Command(
        name="test",
        argv=("pytest", "tests"),
        working_directory="tools",
        created_at=original.created_at,
    )
    assert row == (
        '["pytest", "tests"]',
        "tools",
        original.created_at,
    )


def test_edit_command_can_change_one_field_and_rejects_no_change(
    tmp_path: Path,
) -> None:
    """Editing can preserve one field but rejects a request with no changes."""
    root = _initialise_repository(tmp_path / "repository")
    repository = _repository(root)
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        original = add_command(connection, repository, "test", ["pytest"], ".")
        edited = edit_command(connection, repository, "test", cwd=".")

        with pytest.raises(CommandError, match="must change"):
            edit_command(connection, repository, "test")

        with pytest.raises(CommandError, match="at least one argument"):
            edit_command(connection, repository, "test", [])
    finally:
        connection.close()

    assert edited.argv == original.argv
    assert edited.working_directory == "."
    assert edited.created_at == original.created_at


def test_edit_command_rejects_unknown_name(tmp_path: Path) -> None:
    """Editing an unknown name raises the command-specific not-found error."""
    root = _initialise_repository(tmp_path / "repository")
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        with pytest.raises(CommandNotFoundError, match="missing"):
            edit_command(connection, _repository(root), "missing", ["echo"])
    finally:
        connection.close()


def test_rename_command_changes_name_and_rejects_collisions(
    tmp_path: Path,
) -> None:
    """Renaming preserves the command record and refuses an existing name."""
    root = _initialise_repository(tmp_path / "repository")
    repository = _repository(root)
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        original = add_command(connection, repository, "test", ["pytest"])
        add_command(connection, repository, "lint", ["ruff"])
        renamed = rename_command(connection, repository, "test", "check")

        with pytest.raises(CommandError, match="already exists"):
            rename_command(connection, repository, "check", "lint")
    finally:
        connection.close()

    assert renamed.name == "check"
    assert renamed.argv == original.argv
    assert renamed.working_directory == original.working_directory
    assert renamed.created_at == original.created_at


def test_remove_command_deletes_only_the_repository_command(
    tmp_path: Path,
) -> None:
    """Removing deletes a command and leaves another repository's command alone."""
    root = _initialise_repository(tmp_path / "repository")
    repository = _repository(root)
    other_repository = Repository(root=root, id="other-repository")
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        add_command(connection, repository, "test", ["pytest"])
        add_command(connection, other_repository, "test", ["other"])
        remove_command(connection, repository, "test")
        commands = list_commands(connection, other_repository)

        with pytest.raises(CommandNotFoundError, match="test"):
            remove_command(connection, repository, "test")
    finally:
        connection.close()

    assert [command.argv for command in commands] == [("other",)]


def test_add_command_rejects_unsafe_or_missing_directories(tmp_path: Path) -> None:
    """Adding a command rejects directories outside or missing from the repository."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "file").write_text("not a directory")
    repository = _repository(root)
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        with pytest.raises(CommandError, match="inside the repository"):
            add_command(connection, repository, "outside", ["echo"], "../outside")

        with pytest.raises(CommandError, match="does not exist"):
            add_command(connection, repository, "missing", ["echo"], "missing")

        with pytest.raises(CommandError, match="not a directory"):
            add_command(connection, repository, "file", ["echo"], "file")
    finally:
        connection.close()


def test_add_command_rejects_duplicate_name_with_edit_hint(tmp_path: Path) -> None:
    """Adding an existing name points to the future edit command."""
    root = _initialise_repository(tmp_path / "repository")
    repository = _repository(root)
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        add_command(connection, repository, "test", ["pytest"])

        with pytest.raises(CommandError, match="agent-run edit test"):
            add_command(connection, repository, "test", ["other"])
    finally:
        connection.close()


def test_cli_add_and_list_support_text_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add and list subcommands use the shared text and JSON envelopes."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "scripts").mkdir()
    database_path = tmp_path / "agent-run.db"
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(database_path))

    add_exit_code = main(["add", "build", "--cwd", "scripts", "--", "make", "all"])
    add_output = capsys.readouterr()

    assert add_exit_code == 0
    assert "name: build" in add_output.out
    assert "working directory: scripts" in add_output.out
    assert 'argv: ["make", "all"]' in add_output.out
    assert add_output.err == ""

    list_exit_code = main(["list", "--json"])
    list_output = capsys.readouterr()
    result = json.loads(list_output.out)

    assert list_exit_code == 0
    assert result == {
        "ok": True,
        "data": {
            "commands": [
                {
                    "name": "build",
                    "working_directory": "scripts",
                    "argv": ["make", "all"],
                }
            ]
        },
    }
    assert list_output.err == ""


def test_cli_list_rejects_separator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The list command rejects a command separator."""
    with pytest.raises(SystemExit) as error:
        main(["list", "--json", "--", "x"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert result["error"]["message"] == (
        "the -- separator is only valid for the add or edit commands"
    )
    assert "agent-run: error:" in captured.err


def test_cli_edit_rename_and_remove_support_text_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The edit, rename, and remove subcommands update the registered command."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "scripts").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    main(["add", "build", "--", "make"])
    capsys.readouterr()

    edit_exit_code = main(
        [
            "edit",
            "build",
            "--cwd",
            "scripts",
            "--json",
            "--",
            "make",
            "all",
        ]
    )
    edit_output = capsys.readouterr()
    edit_result = json.loads(edit_output.out)

    assert edit_exit_code == 0
    assert edit_result == {
        "ok": True,
        "data": {
            "name": "build",
            "working_directory": "scripts",
            "argv": ["make", "all"],
        },
    }
    assert edit_output.err == ""

    rename_exit_code = main(["rename", "build", "check"])
    rename_output = capsys.readouterr()

    assert rename_exit_code == 0
    assert "name: check" in rename_output.out
    assert "working directory: scripts" in rename_output.out
    assert 'argv: ["make", "all"]' in rename_output.out
    assert rename_output.err == ""

    remove_exit_code = main(["remove", "check", "--json"])
    remove_output = capsys.readouterr()
    remove_result = json.loads(remove_output.out)

    assert remove_exit_code == 0
    assert remove_result == {"ok": True, "data": {"name": "check"}}
    assert remove_output.err == ""


@pytest.mark.parametrize(
    "arguments",
    [
        ["edit", "missing", "--json", "--", "echo"],
        ["rename", "missing", "renamed", "--json"],
        ["remove", "missing", "--json"],
    ],
)
def test_cli_mutations_report_unknown_names_as_not_found(
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mutation subcommands map unknown names to the not-found error."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main(arguments)

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 1
    assert result["ok"] is False
    assert result["error"]["code"] == "not-found"
    assert "missing" in result["error"]["message"]
    assert captured.err == ""


@pytest.mark.parametrize(
    ("arguments", "command_names", "message", "raises_system_exit"),
    [
        (
            ["edit", "build", "--json"],
            ["build"],
            "Edit must change arguments or working directory.",
            False,
        ),
        (
            ["edit", "build", "stray", "--json"],
            ["build"],
            "all command arguments must follow --",
            True,
        ),
        (
            ["rename", "a", "b", "--json"],
            ["a", "b"],
            'Command "b" already exists. Use agent-run edit b to change it.',
            False,
        ),
        (
            ["rename", "a", " ", "--json"],
            ["a"],
            "Command name must not be empty.",
            False,
        ),
    ],
)
def test_cli_mutations_report_usage_errors(
    arguments: list[str],
    command_names: list[str],
    message: str,
    raises_system_exit: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mutation subcommands report invalid requests as usage errors."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    for name in command_names:
        main(["add", name, "--", "echo"])
    capsys.readouterr()

    if raises_system_exit:
        with pytest.raises(SystemExit) as error:
            main(arguments)

        exit_code = error.value.code
    else:
        exit_code = main(arguments)

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 2
    assert result == {
        "ok": False,
        "error": {"code": "usage", "message": message},
    }

    if raises_system_exit:
        assert "agent-run edit: error:" in captured.err
    else:
        assert captured.err == ""


@pytest.mark.parametrize(
    ("command", "description"),
    [
        ("edit", "Change a named project command."),
        ("rename", "Rename a named project command."),
        ("remove", "Remove a named project command."),
    ],
)
def test_cli_mutation_help_uses_json_envelope(
    command: str,
    description: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation help is returned inside the JSON success envelope."""
    exit_code = main([command, "--help", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert description in result["data"]["help"]
    assert captured.err == ""


def test_cli_add_help_uses_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Add help is returned inside the JSON success envelope."""
    exit_code = main(["add", "--help", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert "Register a named project command." in result["data"]["help"]
    assert captured.err == ""


def test_cli_list_help_uses_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """List help is returned inside the JSON success envelope."""
    exit_code = main(["list", "--help", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 0
    assert result["ok"] is True
    assert "List named project commands in name order." in result["data"]["help"]
    assert captured.err == ""


def test_cli_add_validation_error_uses_json_usage_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add command reports invalid directories as JSON usage errors."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main(["add", "build", "--cwd", "../outside", "--json", "--", "make"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "inside the repository" in result["error"]["message"]
    assert captured.err == ""


def test_cli_add_preserves_command_arguments_starting_with_double_dash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command argument array can begin with another `--` separator."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main(["add", "build", "--", "--", "x"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.startswith("name: build")
    assert 'argv: ["--", "x"]' in captured.out
    assert captured.err == ""


def test_cli_add_accepts_abbreviated_cwd_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add command accepts argparse's abbreviated cwd option."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "tools").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main(["add", "build", "--cw", "tools", "--", "x"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "working directory: tools" in captured.out
    assert 'argv: ["x"]' in captured.out
    assert captured.err == ""


def test_cli_add_keeps_json_flag_in_command_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `--json` after the separator remains a command argument in text mode."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    exit_code = main(["add", "a", "--", "echo", "--json"])

    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.startswith("name: a")
    assert 'argv: ["echo", "--json"]' in captured.out
    assert captured.err == ""


def test_cli_add_rejects_arguments_before_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add command rejects arguments that appear before `--`."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    with pytest.raises(SystemExit) as error:
        main(["add", "build", "make", "--json", "--", "x"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "follow --" in result["error"]["message"]
    assert "agent-run add: error:" in captured.err


def test_cli_add_requires_name_before_separator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add command requires a name before `--`."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    with pytest.raises(SystemExit) as error:
        main(["add", "--json", "--", "make"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "required: name" in result["error"]["message"]
    assert "agent-run add: error:" in captured.err


def test_cli_add_requires_name_before_separator_with_multiple_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add command requires a name before `--` with multiple arguments."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    with pytest.raises(SystemExit) as error:
        main(["add", "--json", "--", "make", "all"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "required: name" in result["error"]["message"]
    assert "agent-run add: error:" in captured.err


def test_cli_add_requires_name_when_cwd_matches_first_command_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add command ignores the `--cwd` value when checking for a name."""
    root = _initialise_repository(tmp_path / "repository")
    (root / "build").mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    with pytest.raises(SystemExit) as error:
        main(["add", "--cwd", "build", "--json", "--", "build"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert error.value.code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "required: name" in result["error"]["message"]
    assert "agent-run add: error:" in captured.err


def test_cli_list_newer_schema_error_uses_json_environment_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The list command reports a newer database schema as an environment error."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    def fail_connect_database() -> NoReturn:
        """Fail as if the database schema is newer than this release."""
        raise schema.NewerSchemaError("database schema is newer than supported")

    monkeypatch.setattr("agent_run.cli.connect_database", fail_connect_database)

    exit_code = main(["list", "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert result == {
        "ok": False,
        "error": {
            "code": "environment",
            "message": "database schema is newer than supported",
        },
    }
    assert captured.err == ""


def test_cli_add_sqlite_error_uses_json_environment_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The add command reports database failures as environment errors."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    def fail_connect_database() -> NoReturn:
        """Fail as if the database cannot be opened."""
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr("agent_run.cli.connect_database", fail_connect_database)

    exit_code = main(["add", "build", "--json", "--", "make"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 3
    assert result == {
        "ok": False,
        "error": {
            "code": "environment",
            "message": "database unavailable",
        },
    }
    assert captured.err == ""


def test_add_command_rejects_symlinked_directory_outside_repository(
    tmp_path: Path,
) -> None:
    """Adding a symlink to a directory outside the repository is refused."""
    root = _initialise_repository(tmp_path / "repository")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    repository = _repository(root)
    connection = connect_database(tmp_path / "agent-run.db")
    try:
        with pytest.raises(CommandError, match="inside the repository"):
            add_command(connection, repository, "linked", ["echo"], "linked")
    finally:
        connection.close()


def test_cli_duplicate_name_uses_json_usage_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A duplicate name returns a JSON usage error with its edit command."""
    root = _initialise_repository(tmp_path / "repository")
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGENT_RUN_DATABASE", str(tmp_path / "agent-run.db"))

    main(["add", "build", "--", "make"])
    capsys.readouterr()
    exit_code = main(["add", "build", "--json", "--", "other"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "usage"
    assert "agent-run edit build" in result["error"]["message"]
    assert captured.err == ""
