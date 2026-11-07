"""Check draft behaviour with invented worktrees and progress records."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import review_packet.cli as cli
import review_packet.progress_records as records
from review_packet.brief import render
from review_packet.cli import main
from review_packet.imports import find_imports
from review_packet.manifest import create as create_manifest
from review_packet.manifest import to_json as manifest_to_json
from review_packet.progress_records import (
	Chunk,
	ProgressError,
	RelatedTasks,
	Task,
	TaskChunk,
	read_chunk,
	read_related_tasks,
)
from review_packet.scan import Finding
from review_packet.selection import (
	MissingIncludeError,
	OutsideSymlinkError,
	select_files,
)
from review_packet.settings import InvalidSettingsError, read_settings
from review_packet.snapshot import NO_COMMITS, capture


def git(root: Path, *args: str) -> None:
	"""Run a Git command in an invented repository."""
	subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _caller_lines(brief: str) -> list[str]:
	"""Return the lines of a draft brief's Callers section."""
	return (
		brief.split("### Callers\n\n", 1)[1]
		.split("\n\n### Left out", 1)[0]
		.splitlines()
	)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
	"""Create a small committed repository with predictable identities."""
	git(tmp_path, "init", "-q")
	git(tmp_path, "config", "user.email", "review@example.invalid")
	git(tmp_path, "config", "user.name", "Reviewer")
	(tmp_path / "old.txt").write_text("old\n")
	(tmp_path / "delete.txt").write_text("delete\n")
	(tmp_path / "README.md").write_text("Repository\n")
	git(tmp_path, "add", ".")
	git(tmp_path, "commit", "-qm", "initial")
	return tmp_path


@pytest.fixture
def draft_context(
	repo: Path,
	tmp_path_factory: pytest.TempPathFactory,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Use one invented chunk and a draft folder outside its repository."""
	# The invented progress chunk for draft tests.
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.setattr(
		cli.tempfile, "gettempdir", lambda: str(tmp_path_factory.mktemp("drafts"))
	)
	monkeypatch.chdir(repo)


def test_draft_refuses_secret_in_selected_file(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Refuse a selected file with a credential without writing a draft."""
	# An invented credential that must never appear in the output.
	secret = "AKIA" + "1234567890123456"
	(repo / "old.txt").write_text(f"safe\n{secret}\n")
	# The draft destination lives outside the invented repository.
	brief_path = cli._brief_path(repo, "chk_example")

	assert main(["draft", "chk_example"]) == 1

	output = capsys.readouterr()

	assert output.err.splitlines() == [
		"Draft not written: these files hold secrets or local paths.",
		"old.txt:2: AWS key",
	]
	assert secret not in output.out + output.err
	assert not brief_path.exists()
	assert not brief_path.with_name("manifest.json").exists()


def test_draft_refuses_secret_in_rendered_brief(
	repo: Path,
	draft_context: None,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Refuse a credential copied into the brief without writing a draft."""
	# The brief copies the task overview from the progress record.
	secret = "AKIA" + "1234567890123456"
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", f"Overview {secret}", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	# The draft destination lives outside the invented repository.
	brief_path = cli._brief_path(repo, "chk_example")

	assert main(["draft", "chk_example"]) == 1

	output = capsys.readouterr()

	assert output.err.splitlines()[0] == (
		"Draft not written: these files hold secrets or local paths."
	)
	assert output.err.splitlines()[1].startswith("REVIEW.md:")
	assert secret not in output.out + output.err
	assert not brief_path.exists()
	assert not brief_path.with_name("manifest.json").exists()


def test_draft_json_refusal_has_structured_findings_without_matched_text(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Return safe finding fields in the JSON error data."""
	# A changed file with an invented credential.
	secret = "AKIA" + "1234567890123456"
	(repo / "old.txt").write_text(f"{secret}\n")

	assert main(["draft", "chk_example", "--json"]) == 1

	output = capsys.readouterr()
	error = json.loads(output.out)["error"]

	assert error == {
		"code": "check-failed",
		"message": "Draft not written: these files hold secrets or local paths.",
		"data": {"findings": [{"path": "old.txt", "line": 1, "kind": "AWS key"}]},
	}
	assert secret not in output.out + output.err


def test_scan_error_has_short_summary() -> None:
	"""Give callers a useful exception message without listing findings."""
	findings = (Finding("notes.txt", 2, "AWS key"),)

	assert (
		str(cli.ScanError(findings))
		== "Draft refused: secrets or local paths found (1)"
	)


def test_snapshot_covers_renames_deletions_untracked_and_base(repo: Path) -> None:
	"""Keep each change kind visible and include commits since a base."""
	base = subprocess.check_output(
		["git", "rev-parse", "HEAD"], cwd=repo, text=True
	).strip()

	git(repo, "mv", "old.txt", "new.txt")
	git(repo, "rm", "delete.txt")
	git(repo, "commit", "-qm", "move and delete")
	(repo / "extra.txt").write_text("new\n")

	snapshot = capture(repo, base)

	assert {(item.path, item.status, item.old_path) for item in snapshot.changes} == {
		("new.txt", "renamed", "old.txt"),
		("delete.txt", "deleted", None),
		("extra.txt", "untracked", None),
	}
	assert snapshot.uncommitted
	assert capture(repo).changes[0].path == "extra.txt"


def test_settings_absent_and_present(repo: Path) -> None:
	"""Use empty settings by default and read invented include values."""
	assert not read_settings(repo).found

	(repo / ".review-packet.toml").write_text(
		'checks = ["test"]\ninclude = ["README.md"]\n'
	)
	settings = read_settings(repo)

	assert settings.found
	assert settings.checks == ("test",)
	assert settings.include == ("README.md",)


def test_progress_uses_temporary_database(
	repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Read an invented task and chunk through progress JSON."""
	database = tmp_path / "progress.db"
	monkeypatch.setenv("AGENTS_PROGRESS_DATABASE", str(database))

	def progress(*arguments: str) -> dict:
		"""Run progress against the temporary database and return its data."""
		result = subprocess.run(
			["progress", *arguments, "--json"],
			cwd=repo,
			capture_output=True,
			text=True,
			check=True,
		)

		return json.loads(result.stdout)["data"]

	progress("project", "init", "--slug", "invented", "--name", "Invented")
	task = progress(
		"task",
		"add",
		"--slug",
		"review",
		"--title",
		"Review",
		"--overview",
		"Invented overview",
		"--done-when",
		"Done",
	)
	chunk = progress(
		"chunk",
		"add",
		"--task",
		task["id"],
		"--title",
		"Draft",
		"--description",
		"Draft a brief",
		"--review-question",
		"Is it complete?",
	)
	progress(
		"task",
		"add",
		"--slug",
		"follow-up",
		"--title",
		"Follow-up",
		"--overview",
		"Pending work",
		"--done-when",
		"Done",
		"--depends-on",
		task["id"],
	)

	assert read_chunk(repo, chunk["id"]).task.title == "Review"
	assert read_related_tasks(repo, task["id"]) == RelatedTasks((), ("Follow-up",))


def test_draft_json_include_and_force(
	repo: Path,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Write both draft files and replace both only when force is requested."""
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "drafts"))
	monkeypatch.chdir(repo)

	assert main(["draft", "chk_example", "--include", "README.md", "--json"]) == 0

	data = json.loads(capsys.readouterr().out)["data"]

	assert Path(data["brief"]).is_absolute()
	assert Path(data["manifest"]).is_absolute()
	assert "README.md" in data["contents"]
	assert Path(data["brief"]).exists()
	assert Path(data["manifest"]).exists()
	assert "Content fingerprint" not in Path(data["brief"]).read_text()

	Path(data["brief"]).write_text("Edited brief\n")
	Path(data["manifest"]).write_text("Edited manifest\n")

	assert main(["draft", "chk_example", "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
	assert Path(data["brief"]).read_text() == "Edited brief\n"
	assert Path(data["manifest"]).read_text() == "Edited manifest\n"

	assert main(["draft", "chk_example", "--force", "--json"]) == 0
	assert json.loads(capsys.readouterr().out)["ok"] is True
	assert Path(data["brief"]).read_text() != "Edited brief\n"
	assert json.loads(Path(data["manifest"]).read_text())["files"]


def test_draft_adds_direct_imports_and_changed_files_tests(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Hash direct imports and matched tests, and report missing local imports."""
	# The package containing the changed source file and its tests.
	package = repo / "app"
	(package / "src").mkdir(parents=True)
	(package / "tests").mkdir()
	(package / "package.json").write_text('{"imports": {"#src/*": "./src/*.ts"}}')
	(package / "src" / "helper.ts").write_text('import "./deep";\n')
	(package / "parent.ts").write_text("")
	(package / "src" / "deep.ts").write_text("export const deep = true;\n")
	(package / "src" / "changed.ts").write_text(
		'import "#src/helper";\nimport "../parent";\nimport "./missing";\n'
	)
	(package / "tests" / "changed.test.ts").write_text("test('changed', () => {});\n")
	(package / "tests" / "helper.test.ts").write_text("test('helper', () => {});\n")
	git(repo, "add", "app")
	git(repo, "commit", "-qm", "add app")
	(package / "src" / "changed.ts").write_text(
		'import "#src/helper";\nimport "../parent";\nimport "./missing";\n'
		"export const changed = true;\n"
	)

	assert main(["draft", "chk_example", "--json"]) == 0

	# The draft command's result.
	data = json.loads(capsys.readouterr().out)["data"]
	# Selected paths in the draft.
	contents = set(data["contents"])
	# The generated manifest beside the brief.
	manifest = json.loads(Path(data["manifest"]).read_text())
	# The generated brief outside the invented repository.
	brief = Path(data["brief"]).read_text()

	assert {
		"app/src/changed.ts",
		"app/src/helper.ts",
		"app/parent.ts",
		"app/tests/changed.test.ts",
	} <= contents
	assert "app/src/deep.ts" not in contents
	assert "app/tests/helper.test.ts" not in contents
	assert "app/src/helper.ts" in {item["path"] for item in manifest["files"]}
	assert "app/src/changed.ts: ./missing (unresolved import)" in brief


def test_draft_lists_same_package_callers_without_selecting_them(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""List tracked and new callers in the same package, even one already in the packet."""
	# The package holding the changed file and its callers.
	package = repo / "app"
	# A second package whose importer should not be listed.
	other = repo / "other"
	package.mkdir()
	other.mkdir()
	(package / "package.json").write_text("{}")
	(other / "package.json").write_text("{}")
	(package / ".gitignore").write_text("ignored.ts\n")
	(package / "changed.ts").write_text('import "./included";\n')
	(package / "included.ts").write_text('import "./changed";\n')
	(package / "caller.ts").write_text('import "./changed";\n')
	(other / "caller.ts").write_text('import "../app/changed";\n')
	git(repo, "add", "app", "other")
	git(repo, "commit", "-qm", "add callers")
	(package / "changed.ts").write_text(
		'import "./included";\nexport const changed = 1;\n'
	)
	(package / "new.ts").write_text('import "./changed";\n')
	(package / "ignored.ts").write_text('import "./changed";\n')

	assert main(["draft", "chk_example", "--json"]) == 0

	# The draft's JSON result.
	data = json.loads(capsys.readouterr().out)["data"]
	# The brief the draft wrote.
	brief = Path(data["brief"]).read_text()
	# The caller paths in the brief.
	callers = _caller_lines(brief)
	# The manifest the draft wrote.
	manifest = json.loads(Path(data["manifest"]).read_text())

	assert callers == [
		"- app/caller.ts",
		"- app/included.ts",
		"- app/new.ts",
	]
	assert "app/included.ts" in data["contents"]
	assert "app/caller.ts" not in data["contents"]
	assert "app/new.ts" in data["contents"]
	assert "app/caller.ts" not in {item["path"] for item in manifest["files"]}


def test_draft_lists_callers_of_deleted_python_and_typescript_files(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""List importers of deleted files without adding the deleted files anywhere else."""
	# The Python package that loses a module.
	python_package = repo / "python"
	# The Python import package inside it.
	module = python_package / "src" / "app"
	module.mkdir(parents=True)
	(python_package / "pyproject.toml").write_text("[project]\nname = 'app'\n")
	(module / "__init__.py").write_text("\n")
	(module / "removed.py").write_text("value = 1\n")
	(module / "caller.py").write_text("from app import removed\n")
	# The TypeScript package that loses a file.
	js_package = repo / "javascript"
	js_package.mkdir()
	(js_package / "package.json").write_text("{}")
	(js_package / "removed.ts").write_text("export const value = 1;\n")
	(js_package / "caller.ts").write_text('import "./removed";\n')
	git(repo, "add", "python", "javascript")
	git(repo, "commit", "-qm", "add files that will be deleted")
	git(repo, "rm", "python/src/app/removed.py", "javascript/removed.ts")

	assert main(["draft", "chk_example", "--json"]) == 0

	# The draft's JSON result.
	data = json.loads(capsys.readouterr().out)["data"]
	# The brief the draft wrote.
	brief = Path(data["brief"]).read_text()
	# The caller paths in the brief.
	callers = _caller_lines(brief)
	# The manifest the draft wrote.
	manifest = json.loads(Path(data["manifest"]).read_text())
	# The Python caller's imports when followed forward for the packet.
	python_imports = find_imports(repo, "python/src/app/caller.py")
	# The TypeScript caller's imports when followed forward for the packet.
	js_imports = find_imports(repo, "javascript/caller.ts")

	assert callers == [
		"- javascript/caller.ts",
		"- python/src/app/caller.py",
	]
	assert "python/src/app/removed.py" not in python_imports.paths
	assert python_imports.paths == ("python/src/app/__init__.py",)
	assert "javascript/removed.ts" not in js_imports.paths
	assert "./removed" in js_imports.unresolved
	assert "python/src/app/removed.py" not in data["contents"]
	assert "javascript/removed.ts" not in data["contents"]
	assert {item["path"] for item in manifest["changes"]} >= {
		"python/src/app/removed.py",
		"javascript/removed.ts",
	}
	assert not {item["path"] for item in manifest["files"]} & {
		"python/src/app/removed.py",
		"javascript/removed.ts",
	}


def test_draft_lists_a_caller_of_a_deleted_package_file(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""List a relative importer when the package file it imports is deleted."""
	# The Python package that loses its entry file.
	package = repo / "project"
	# The import package holding the relative caller.
	module = package / "src" / "app"
	module.mkdir(parents=True)
	(package / "pyproject.toml").write_text("[project]\nname = 'app'\n")
	(module / "__init__.py").write_text("name = 1\n")
	(module / "caller.py").write_text("from . import name\n")
	git(repo, "add", "project")
	git(repo, "commit", "-qm", "add package and caller")
	git(repo, "rm", "project/src/app/__init__.py")

	assert main(["draft", "chk_example", "--json"]) == 0

	# The draft's JSON result.
	data = json.loads(capsys.readouterr().out)["data"]
	# The brief the draft wrote.
	brief = Path(data["brief"]).read_text()
	# The manifest the draft wrote.
	manifest = json.loads(Path(data["manifest"]).read_text())
	# The deleted package file.
	deleted = "project/src/app/__init__.py"

	assert _caller_lines(brief) == ["- project/src/app/caller.py"]
	assert deleted not in data["contents"]
	assert deleted not in find_imports(repo, "project/src/app/caller.py").paths
	assert deleted not in {item["path"] for item in manifest["files"]}


def test_draft_lists_importers_of_a_renamed_python_file(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""List files that still import a renamed module by its old name."""
	# The package holding the renamed module.
	package = repo / "project"
	# The Python import package inside it.
	module = package / "src" / "app"
	module.mkdir(parents=True)
	(package / "pyproject.toml").write_text("[project]\nname = 'app'\n")
	(module / "__init__.py").write_text("\n")
	(module / "old.py").write_text("value = 1\n")
	(module / "caller.py").write_text("from app import old\n")
	git(repo, "add", "project")
	git(repo, "commit", "-qm", "add old module")
	git(repo, "mv", "project/src/app/old.py", "project/src/app/new.py")

	assert main(["draft", "chk_example", "--json"]) == 0

	# The draft's JSON result.
	data = json.loads(capsys.readouterr().out)["data"]
	# The brief the draft wrote.
	brief = Path(data["brief"]).read_text()
	# The caller paths in the brief.
	callers = _caller_lines(brief)
	# The manifest the draft wrote.
	manifest = json.loads(Path(data["manifest"]).read_text())
	# The manifest's changed files, by path.
	changes = {item["path"]: item for item in manifest["changes"]}

	assert changes["project/src/app/new.py"]["old_path"] == "project/src/app/old.py"
	assert callers == ["- project/src/app/caller.py"]
	assert "project/src/app/caller.py" not in data["contents"]
	assert "project/src/app/old.py" not in data["contents"]
	assert "project/src/app/caller.py" not in {
		item["path"] for item in manifest["files"]
	}


def test_draft_says_when_no_callers_were_found(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Say so in the brief when no file imports a changed file."""
	(repo / "old.txt").write_text("changed\n")

	assert main(["draft", "chk_example", "--json"]) == 0

	# The draft's JSON result.
	data = json.loads(capsys.readouterr().out)["data"]
	# The brief the draft wrote.
	brief = Path(data["brief"]).read_text()

	assert "### Callers\n\nNo callers found.\n\n### Left out" in brief


def test_draft_lists_unparseable_python_file_separately(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""List a changed file whose imports cannot be read as its own omission."""
	# The changed Python file with invalid syntax.
	source = repo / "changed.py"
	source.write_text("from package import\n")

	assert main(["draft", "chk_example", "--json"]) == 0

	# The result printed by the draft command.
	data = json.loads(capsys.readouterr().out)["data"]
	# The generated brief, written outside the invented repository.
	brief = Path(data["brief"]).read_text()

	assert "- changed.py (could not read imports)" in brief
	assert "changed.py (unresolved import)" not in brief


def test_draft_leaves_out_linked_import_and_test_but_refuses_included_link(
	repo: Path,
	draft_context: None,
	tmp_path_factory: pytest.TempPathFactory,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""List discovered outside links while refusing one the user includes."""
	# Files outside the invented repository that the packet cannot share.
	outside_folder = tmp_path_factory.mktemp("outside")
	(outside_folder / "helper.ts").write_text("export const helper = 1\n")
	(outside_folder / "changed.test.ts").write_text("test('changed', () => {})\n")
	# The project with links to an imported file and a matching test.
	package = repo / "app"
	(package / "tests").mkdir(parents=True)
	(package / "package.json").write_text("{}")
	(package / "changed.ts").write_text('import "./helper";\n')
	(package / "helper.ts").symlink_to(outside_folder / "helper.ts")
	(package / "tests" / "changed.test.ts").symlink_to(
		outside_folder / "changed.test.ts"
	)
	git(repo, "add", "app")
	git(repo, "commit", "-qm", "add linked project")
	(package / "changed.ts").write_text(
		'import "./helper";\nexport const changed = 1;\n'
	)

	assert main(["draft", "chk_example", "--json"]) == 0

	# The draft result printed by the command.
	data = json.loads(capsys.readouterr().out)["data"]
	# The brief written outside the invented repository.
	brief = Path(data["brief"]).read_text()

	assert "app/helper.ts" not in data["contents"]
	assert "app/tests/changed.test.ts" not in data["contents"]
	assert "- app/helper.ts (links outside the repository)" in brief
	assert "- app/tests/changed.test.ts (links outside the repository)" in brief

	with pytest.raises(OutsideSymlinkError, match="app/helper.ts"):
		select_files(repo, capture(repo), ("app/helper.ts",))


def test_selects_tsx_tests_for_changed_component(repo: Path) -> None:
	"""Include a same-name TSX test beside a changed component."""
	# The project holding the component.
	package = repo / "app"
	# The folder with the component and the test beside it.
	source_folder = package / "src"
	source_folder.mkdir(parents=True)
	(package / "package.json").write_text("{}")
	(source_folder / "Button.tsx").write_text("export const Button = 1\n")
	(source_folder / "Button.test.tsx").write_text("test('Button', () => {})\n")
	git(repo, "add", "app")
	git(repo, "commit", "-qm", "add component")
	(source_folder / "Button.tsx").write_text("export const Button = 2\n")

	# Files selected for the changed component.
	selection = select_files(repo, capture(repo), ())

	assert "app/src/Button.test.tsx" in selection.files


def test_does_not_select_ignored_or_neighbouring_tests(repo: Path) -> None:
	"""Keep ignored files and another component's tests out of a draft."""
	# A root project with unrelated same-name tests.
	(repo / "package.json").write_text("{}")
	(repo / ".gitignore").write_text("tests/generated/\n")
	# The folder holding one of the changed files.
	source_folder = repo / "src"
	# Another component's folder, whose test has the same name.
	other_folder = source_folder / "other"
	other_folder.mkdir(parents=True)
	(source_folder / "index.ts").write_text("export const value = 1\n")
	(other_folder / "index.test.ts").write_text("test('other', () => {})\n")
	git(repo, "add", ".")
	git(repo, "commit", "-qm", "add source and tests")
	# A matching test inside a searched folder, ignored by Git.
	ignored_folder = repo / "tests" / "generated"
	ignored_folder.mkdir(parents=True)
	(ignored_folder / "index.test.ts").write_text("test('ignored', () => {})\n")
	(source_folder / "index.ts").write_text("export const value = 2\n")

	# Files selected for the changed file.
	selection = select_files(repo, capture(repo), ())

	assert "src/other/index.test.ts" not in selection.files
	assert "tests/generated/index.test.ts" not in selection.files


@pytest.mark.parametrize("manifest_text", ('{"imports":', '{"imports": []}'))
def test_draft_lists_alias_when_package_manifest_is_invalid(
	repo: Path,
	draft_context: None,
	capsys: pytest.CaptureFixture[str],
	manifest_text: str,
) -> None:
	"""Keep drafting and list an alias the package manifest cannot resolve."""
	# The changed JavaScript file and its package manifest.
	package = repo / "app"
	package.mkdir()
	(package / "changed.ts").write_text('import "#src/helper";\n')
	(package / "package.json").write_text(manifest_text)

	assert main(["draft", "chk_example", "--json"]) == 0

	# The result printed by the draft command.
	data = json.loads(capsys.readouterr().out)["data"]
	# The generated brief, written outside the invented repository.
	brief = Path(data["brief"]).read_text()

	assert "app/changed.ts: #src/helper (unresolved import)" in brief


def test_snapshot_covers_staged_and_unstaged_edit_without_base(repo: Path) -> None:
	"""Include both index and worktree edits when the base defaults to HEAD."""
	(repo / "old.txt").write_text("staged\n")
	git(repo, "add", "old.txt")
	(repo / "old.txt").write_text("staged and unstaged\n")

	snapshot = capture(repo)

	assert [change.path for change in snapshot.changes] == ["old.txt"]
	assert snapshot.uncommitted

	assert (
		snapshot.base_sha
		== subprocess.check_output(
			["git", "rev-parse", "HEAD"], cwd=repo, text=True
		).strip()
	)


def test_manifest_fingerprint_changes_with_file_contents(repo: Path) -> None:
	"""Keep the fingerprint until a selected file's contents change."""
	(repo / "old.txt").write_text("first edit\n")
	snapshot = capture(repo)
	contents = select_files(repo, snapshot, ()).files
	first = create_manifest(repo, snapshot, contents)

	assert create_manifest(repo, snapshot, contents).fingerprint == first.fingerprint

	(repo / "old.txt").write_text("second edit\n")
	second = create_manifest(repo, capture(repo), contents)

	assert second.fingerprint != first.fingerprint


def test_manifest_hashes_link_text_without_reading_target(
	repo: Path,
	tmp_path_factory: pytest.TempPathFactory,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Changing link text changes the fingerprint without opening its target."""
	outside_folder = tmp_path_factory.mktemp("outside")
	outside = outside_folder / "private-review-target.txt"
	outside.write_text("private content\n")
	link = repo / "linked.txt"
	link.symlink_to(outside)
	first = create_manifest(repo, capture(repo), ("linked.txt",))

	def refuse_read(_path: Path) -> bytes:
		"""Catch any attempt to read a changed file through a link."""
		raise AssertionError("The link target was opened")

	monkeypatch.setattr(Path, "read_bytes", refuse_read)
	assert (
		create_manifest(repo, capture(repo), ("linked.txt",)).fingerprint
		== first.fingerprint
	)

	link.unlink()
	link.symlink_to(outside_folder / "another-private-target.txt")
	assert (
		create_manifest(repo, capture(repo), ("linked.txt",)).fingerprint
		!= first.fingerprint
	)


def test_manifest_covers_explicit_and_ignored_files_but_not_the_brief(
	repo: Path,
	tmp_path_factory: pytest.TempPathFactory,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Track selected files outside Git changes without hashing the editable brief."""
	(repo / ".gitignore").write_text("private-note.txt\n")
	(repo / "private-note.txt").write_text("first note\n")
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	drafts = tmp_path_factory.mktemp("drafts")
	monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(drafts))
	monkeypatch.chdir(repo)
	arguments = [
		"draft",
		"chk_example",
		"--include",
		"README.md",
		"--include",
		"private-note.txt",
		"--json",
	]

	assert main(arguments) == 0

	data = json.loads(capsys.readouterr().out)["data"]
	first = json.loads(Path(data["manifest"]).read_text())
	files = {item["path"]: item["sha256"] for item in first["files"]}

	assert files["README.md"] == hashlib.sha256(b"Repository\n").hexdigest()
	assert files["private-note.txt"] == hashlib.sha256(b"first note\n").hexdigest()
	assert "private-note.txt" not in {item["path"] for item in first["changes"]}

	Path(data["brief"]).write_text("Edited review question\n")

	assert main([*arguments, "--force"]) == 0

	capsys.readouterr()

	assert (
		json.loads(Path(data["manifest"]).read_text())["fingerprint"]
		== first["fingerprint"]
	)

	contents = select_files(
		repo, capture(repo), ("README.md", "private-note.txt")
	).files

	(repo / "README.md").write_text("Changed repository\n")

	assert (
		create_manifest(repo, capture(repo), contents).fingerprint
		!= first["fingerprint"]
	)

	(repo / "README.md").write_text("Repository\n")
	(repo / "private-note.txt").write_text("second note\n")

	assert (
		create_manifest(repo, capture(repo), contents).fingerprint
		!= first["fingerprint"]
	)


def test_manifest_lists_deleted_files_without_a_hash(repo: Path) -> None:
	"""Keep a deletion in the change list without adding it to selected files."""
	(repo / "delete.txt").unlink()
	snapshot = capture(repo)
	manifest = create_manifest(repo, snapshot, select_files(repo, snapshot, ()).files)

	assert {item.path: item.status for item in manifest.changes}[
		"delete.txt"
	] == "deleted"
	assert "delete.txt" not in {item.path for item in manifest.files}
	assert manifest_to_json(manifest)["fingerprint"] == manifest.fingerprint


def test_root_project_manifest_is_selected(repo: Path) -> None:
	"""Find a manifest and test configuration in a root-level project."""
	(repo / "pyproject.toml").write_text(
		"[project]\nname = 'example'\nversion = '0.1'\n"
	)
	(repo / "pytest.ini").write_text("[pytest]\n")
	(repo / "old.txt").write_text("changed\n")

	contents = select_files(repo, capture(repo), ()).files

	assert "pyproject.toml" in contents
	assert "pytest.ini" in contents


def test_includes_are_normalised(repo: Path) -> None:
	"""List a file once when its include uses a leading dot folder."""
	contents = select_files(repo, capture(repo), ("./README.md",)).files

	assert contents.count("README.md") == 1


def test_changed_outside_links_are_all_refused(repo: Path, tmp_path: Path) -> None:
	"""List every changed link that points outside the repository."""
	(repo / "first-link.txt").symlink_to(tmp_path.parent / "outside-one.txt")
	(repo / "second-link.txt").symlink_to(tmp_path.parent / "outside-two.txt")

	with pytest.raises(OutsideSymlinkError) as error:
		select_files(repo, capture(repo), ())

	assert "first-link.txt" in str(error.value)
	assert "second-link.txt" in str(error.value)
	assert "can't be shared" in str(error.value)


def test_included_outside_link_is_refused(repo: Path, tmp_path: Path) -> None:
	"""Give an included outside link the same refusal as a changed link."""
	(repo / "included-link.txt").symlink_to(tmp_path.parent / "outside.txt")

	with pytest.raises(OutsideSymlinkError, match="included-link.txt"):
		select_files(repo, capture(repo), ("included-link.txt",))


def test_included_path_through_outside_link_is_refused(
	repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
	"""Refuse an included file reached through a linked directory."""
	outside_folder = tmp_path_factory.mktemp("outside")
	(outside_folder / "private.txt").write_text("private content\n")
	(repo / "linked-directory").symlink_to(outside_folder, target_is_directory=True)

	with pytest.raises(OutsideSymlinkError, match="linked-directory/private.txt"):
		select_files(repo, capture(repo), ("linked-directory/private.txt",))


def test_inside_link_is_selected(repo: Path) -> None:
	"""Allow a selected link whose target stays in the repository."""
	(repo / "inside-link.txt").symlink_to("README.md")

	assert "inside-link.txt" in select_files(repo, capture(repo), ()).files


def test_draft_reports_outside_link_as_usage(
	repo: Path,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Return usage exit 2 and name the link in the draft error."""
	(repo / "outside-link.txt").symlink_to(tmp_path.parent / "outside.txt")
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.chdir(repo)

	assert main(["draft", "chk_example", "--json"]) == 2

	error = json.loads(capsys.readouterr().out)["error"]
	assert error["code"] == "usage"
	assert "outside-link.txt" in error["message"]


def test_include_outside_repository_is_rejected(repo: Path) -> None:
	"""Reject an included path that leaves the repository."""
	with pytest.raises(MissingIncludeError, match="missing or outside the repository"):
		select_files(repo, capture(repo), ("../outside.txt",))


def test_absolute_include_inside_repository_is_rejected(repo: Path) -> None:
	"""Require included paths to be relative even when they resolve inside."""
	with pytest.raises(MissingIncludeError, match="missing or outside the repository"):
		select_files(repo, capture(repo), (str(repo / "README.md"),))


def test_non_list_settings_are_rejected(repo: Path) -> None:
	"""Reject a checks value that is not a list of strings."""
	(repo / ".review-packet.toml").write_text("checks = 1\n")

	with pytest.raises(InvalidSettingsError):
		read_settings(repo)


def test_unknown_settings_are_rejected(repo: Path) -> None:
	"""Report a misspelt settings key as invalid input."""
	(repo / ".review-packet.toml").write_text('includes = ["README.md"]\n')

	with pytest.raises(InvalidSettingsError, match="includes"):
		read_settings(repo)


def test_draft_text_and_usage_errors(
	repo: Path,
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""Print readable output and reject invalid user arguments with exit 2."""
	chunk = Chunk(
		"chk_example",
		"Draft",
		"Draft a brief",
		"Is it complete?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "drafts"))
	monkeypatch.chdir(repo)
	(repo / ".review-packet.toml").write_text('checks = ["test"]\n')

	assert main(["draft", "chk_example"]) == 0

	output = capsys.readouterr().out

	assert "Brief:" in output
	assert "Manifest:" in output
	assert "Settings: .review-packet.toml" in output

	brief = next((tmp_path / "drafts").rglob("REVIEW.md")).read_text()

	assert "Chunk: Draft" in brief
	assert "Draft a brief" in brief
	assert "Is it complete?" in brief
	assert "Settings: .review-packet.toml" in brief
	assert "### Callers\n\nNo callers found." in brief
	assert "### Left out\n\nNothing left out." in brief

	assert main(["draft", "chk_example", "--base", "missing", "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"

	assert main(["draft", "chk_example", "--include", "missing.txt", "--json"]) == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


def test_null_progress_text_uses_review_prompt(
	repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Render missing progress text without passing null values into Markdown."""
	monkeypatch.setattr(
		records,
		"_read",
		lambda _root, *_args: {
			"id": "chk_null",
			"task_id": "tsk_null",
			"title": "Draft",
			"description": None,
			"review_question": None,
			"task": {
				"id": "tsk_null",
				"title": "Review",
				"overview": None,
				"done_when": [],
				"chunks": [],
			},
		},
	)

	chunk = read_chunk(repo, "chk_null")
	brief = render(chunk, capture(repo), (), (), None, RelatedTasks((), ()), (), (), ())

	assert chunk.description == ""
	assert chunk.task.overview == ""
	assert "## Main review question\n\nWrite this section." in brief


def test_snapshot_marks_staged_new_file_as_added(repo: Path) -> None:
	"""Keep a staged new file distinct from an untracked file."""
	(repo / "staged.txt").write_text("staged\n")
	(repo / "untracked.txt").write_text("untracked\n")
	git(repo, "add", "staged.txt")

	snapshot = capture(repo)

	assert {(change.path, change.status) for change in snapshot.changes} == {
		("staged.txt", "added"),
		("untracked.txt", "untracked"),
	}


def test_snapshot_before_first_commit_marks_index_as_added(tmp_path: Path) -> None:
	"""Show a staged file as added when the repository has no base commit."""
	git(tmp_path, "init", "-q")
	(tmp_path / "staged.txt").write_text("staged\n")
	git(tmp_path, "add", "staged.txt")

	snapshot = capture(tmp_path)
	chunk = Chunk(
		"chk_only",
		"Draft",
		"Draft a brief",
		"",
		Task("tsk_only", "Review", "Overview", (), ()),
	)
	brief = render(chunk, snapshot, (), (), None, RelatedTasks((), ()), (), (), ())

	assert snapshot.base_sha is None
	assert snapshot.changes[0].status == "added"
	assert f"Base commit: {NO_COMMITS}" in brief


def test_snapshot_detects_rename_with_edits(repo: Path) -> None:
	"""Keep the old path when a renamed file also changes."""
	(repo / "old.txt").write_text("same line\n" * 20)
	git(repo, "add", "old.txt")
	git(repo, "commit", "-qm", "expand file")
	git(repo, "mv", "old.txt", "renamed.txt")
	(repo / "renamed.txt").write_text("same line\n" * 19 + "different line\n")

	snapshot = capture(repo)

	assert ("renamed.txt", "renamed", "old.txt") in {
		(change.path, change.status, change.old_path) for change in snapshot.changes
	}


@pytest.mark.parametrize(
	("question", "main_question"),
	[
		("Team question? Outside review: Is this sound?", "Is this sound?"),
		("Is this complete?", "Is this complete?"),
	],
)
def test_brief_uses_outside_review_question_when_marked(
	repo: Path, question: str, main_question: str
) -> None:
	"""Keep the whole recorded question and focus the reviewer on its part."""
	chunk = Chunk(
		"chk_review",
		"Draft",
		"Draft a brief",
		question,
		Task(
			"tsk_review",
			"Review",
			"Overview",
			(),
			(
				TaskChunk("chk_review", "Draft", "active"),
				TaskChunk("chk_next", "Build", "pending"),
			),
		),
	)

	brief = render(chunk, capture(repo), (), (), None, RelatedTasks((), ()), (), (), ())

	assert f"Review question: {question}" in brief
	assert f"## Main review question\n\n{main_question}\n" in brief
	assert "- Build (pending)" in brief
	assert "## Recorded context" in brief


def test_brief_has_no_other_chunks_when_task_has_one(repo: Path) -> None:
	"""Show an explicit empty state for a task with one chunk."""
	chunk = Chunk(
		"chk_only",
		"Draft",
		"Draft a brief",
		"",
		Task("tsk_only", "Review", "Overview", (), ()),
	)

	brief = render(chunk, capture(repo), (), (), None, RelatedTasks((), ()), (), (), ())

	assert "### Other chunks\n\nNone." in brief


def test_brief_marks_agent_sections_and_empty_question(repo: Path) -> None:
	"""Mark each agent section and show "(none)" for a missing review question."""
	chunk = Chunk(
		"chk_only",
		"Draft",
		"Draft a brief",
		"",
		Task("tsk_only", "Review", "Overview", (), ()),
	)

	brief = render(chunk, capture(repo), (), (), None, RelatedTasks((), ()), (), (), ())

	assert "<!-- agent:start review-question -->" in brief
	assert "<!-- agent:end review-question -->" in brief
	assert "Review question: (none)" in brief


def test_brief_ends_with_fixed_file_questions(repo: Path) -> None:
	"""End the brief with the reviewer's file questions, after the agent sections."""
	# A chunk with no recorded details, so the closing section can't depend on them.
	chunk = Chunk(
		"chk_only",
		"Draft",
		"Draft a brief",
		"",
		Task("tsk_only", "Review", "Overview", (), ()),
	)

	# The full text of the drafted brief.
	brief = render(chunk, capture(repo), (), (), None, RelatedTasks((), ()), (), (), ())
	# Everything after the closing heading, which should hold no drafting work.
	closing_section = brief.split("## Files in this packet", 1)[1]

	assert brief.count("## Files in this packet") == 1

	assert brief.rstrip().endswith(
		'Write "None" under either point if nothing applies.'
	)

	assert "Please finish your review by listing:" in closing_section

	assert (
		"- Files you had to ask for, or guess at, because they weren't in this packet."
		in closing_section
	)

	assert "- Files in this packet you didn't need." in closing_section
	assert "<!-- agent:" not in closing_section
	assert "Write this section." not in closing_section

	assert brief.index("<!-- agent:end reviewer-verification -->") < brief.index(
		"## Files in this packet"
	)


def test_argparse_usage_error_is_json(capsys: pytest.CaptureFixture[str]) -> None:
	"""Return a JSON usage envelope even when parsing fails."""
	with pytest.raises(SystemExit) as error:
		main(["draft", "--json"])

	assert error.value.code == 2
	assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"


@pytest.mark.parametrize(
	("code", "exit_code"),
	[
		("usage", 2),
		("invalid-id", 2),
		("wrong-id-type", 2),
		("not-found", 1),
		("environment", 3),
	],
)
def test_progress_error_keeps_its_kind(
	repo: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	code: str,
	exit_code: int,
) -> None:
	"""Map progress error kinds to the shared command-line exit codes."""

	def fail(_root: Path, _id: str) -> Chunk:
		"""Raise the requested progress failure through the draft command."""
		raise ProgressError("Progress failed.", code)

	monkeypatch.setattr(cli, "read_chunk", fail)
	monkeypatch.chdir(repo)

	assert main(["draft", "chk_example", "--json"]) == exit_code
	assert json.loads(capsys.readouterr().out)["error"]["code"] == code


def test_related_tasks_rejects_incomplete_export(
	repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Turn a malformed progress export into a command error."""
	monkeypatch.setattr(
		records, "_read", lambda _root, *_args: {"tasks": [{"title": "Missing ID"}]}
	)

	with pytest.raises(ProgressError, match="incomplete task details"):
		read_related_tasks(repo, "tsk_example")


def test_progress_error_without_object_keeps_fallback(
	repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Report an unusable progress error without a traceback."""
	monkeypatch.setattr(
		records.subprocess,
		"run",
		lambda *_args, **_kwargs: SimpleNamespace(
			stdout='{"ok": false, "error": "broken"}', returncode=1
		),
	)

	with pytest.raises(ProgressError, match="Progress could not read chunk get"):
		read_chunk(repo, "chk_example")
