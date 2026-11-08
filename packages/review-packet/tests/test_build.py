"""Check the archive built from invented draft worktrees."""

import builtins
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

import review_packet.cli as cli
from review_packet.progress_records import Chunk, RelatedTasks, Task


@pytest.fixture
def packet_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	"""Create a committed repository and keep its draft outside the worktree."""
	# The repository used by both draft and build in each test.
	root = tmp_path / "repo"
	root.mkdir()
	for command in (
		("init", "-q"),
		("config", "user.email", "review@example.invalid"),
		("config", "user.name", "Reviewer"),
	):
		subprocess.run(["git", *command], cwd=root, check=True, capture_output=True)
	(root / "README.md").write_text("Repository\n")
	(root / "changed.txt").write_text("before\n")
	subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
	subprocess.run(
		["git", "commit", "-qm", "initial"], cwd=root, check=True, capture_output=True
	)
	(root / "changed.txt").write_text("after\n")
	(root / "new.txt").write_text("new file\n")
	# Progress is replaced because build must not read it.
	chunk = Chunk(
		"chk_example",
		"Build",
		"Build a zip",
		"Is it useful?",
		Task("tsk_example", "Review", "Overview", (), ()),
	)
	monkeypatch.setattr(cli, "read_chunk", lambda _root, _id: chunk)
	monkeypatch.setattr(
		cli, "read_related_tasks", lambda _root, _id: RelatedTasks((), ())
	)
	monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "drafts"))
	monkeypatch.chdir(root)
	return root


def test_build_writes_one_patch_and_full_files_without_running_checks(
	packet_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Package the draft and leave check execution to the draft command."""
	assert cli.main(["draft", "chk_example"]) == 0
	# Build must not call progress or run configured checks.
	monkeypatch.setattr(
		cli, "read_chunk", lambda *_args: pytest.fail("build read progress")
	)
	monkeypatch.setattr(
		cli, "run_checks", lambda *_args: pytest.fail("build ran checks")
	)
	# The explicit destination, so the test never writes to Downloads.
	archive_path = tmp_path / "packet.zip"
	# The author's final wording in the two editable draft documents.
	draft_folder = cli._brief_path(packet_repo, "chk_example").parent
	brief = draft_folder / "REVIEW.md"
	verification = draft_folder / "verification.md"
	brief.write_bytes(brief.read_bytes() + b"\nAuthor's review context.\n")
	verification.write_bytes(verification.read_bytes() + b"\nAuthor's check note.\n")

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 0

	with zipfile.ZipFile(archive_path) as archive:
		# Every entry name in the finished zip.
		names = set(archive.namelist())
		assert {
			"START-HERE.md",
			"REVIEW.md",
			"verification.md",
			"changes.diff",
			"files/changed.txt",
			"files/new.txt",
		} <= names
		assert archive.read("files/changed.txt") == b"after\n"
		assert archive.read("files/new.txt") == b"new file\n"
		assert archive.read("REVIEW.md") == brief.read_bytes()
		assert archive.read("verification.md") == verification.read_bytes()
		# The single patch for files that existed at the base.
		patch = archive.read("changes.diff").decode()
		assert "a/changed.txt" in patch
		assert "new.txt" not in patch
		assert (
			"Answer the main review question" in archive.read("START-HERE.md").decode()
		)
		assert b"under files/" in archive.read("START-HERE.md")
		assert b"End with the line requested in REVIEW.md." in archive.read(
			"START-HERE.md"
		)


@pytest.mark.parametrize(
	"name", ["REVIEW.md", "START-HERE.md", "verification.md", "changes.diff"]
)
def test_build_keeps_generated_documents_when_source_names_match(
	packet_repo: Path, tmp_path: Path, name: str
) -> None:
	"""Keep both generated and repository files with the same root name."""
	(packet_repo / name).write_text(f"Repository {name}\n")
	assert cli.main(["draft", "chk_example"]) == 0
	# The archive under test, outside the source worktree.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 0

	with zipfile.ZipFile(archive_path) as archive:
		assert archive.namelist().count(name) == 1
		assert archive.read(f"files/{name}") == f"Repository {name}\n".encode()
		assert archive.read(name) != archive.read(f"files/{name}")


def test_build_reads_selected_bytes_once(
	packet_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Package the bytes checked by build without rereading a selected file."""
	assert cli.main(["draft", "chk_example"]) == 0
	# The selected file whose reads must be shared by checking, scanning and packing.
	selected_path = packet_repo / "changed.txt"
	# The real built-in open, which the counting wrapper still calls for every file.
	builtin_open = builtins.open
	# The real io.open, which the counting wrapper still calls for every file.
	io_open = io.open
	# Calls to read the selected changed file during build.
	reads = 0

	def record_read(file: str | Path | int, mode: str) -> None:
		"""Count an open of the selected file for reading.

		Args:
			file: The path or file descriptor passed to open.
			mode: The mode passed to open.
		"""
		nonlocal reads

		if (
			isinstance(file, (str, Path))
			and Path(file) == selected_path
			and "r" in mode
		):
			reads += 1

	def count_builtin_open(file, mode="r", *args, **kwargs):
		"""Count reads through the built-in open, then open the file as usual.

		Args:
			file: The path or file descriptor to open.
			mode: The open mode.
			*args: Other positional arguments for open.
			**kwargs: Keyword arguments for open.

		Returns:
			The file object the built-in open returns.
		"""
		record_read(file, mode)
		return builtin_open(file, mode, *args, **kwargs)

	def count_io_open(file, mode="r", *args, **kwargs):
		"""Count reads through io.open, then open the file as usual.

		Args:
			file: The path or file descriptor to open.
			mode: The open mode.
			*args: Other positional arguments for open.
			**kwargs: Keyword arguments for open.

		Returns:
			The file object io.open returns.
		"""
		record_read(file, mode)
		return io_open(file, mode, *args, **kwargs)

	monkeypatch.setattr(builtins, "open", count_builtin_open)
	monkeypatch.setattr(io, "open", count_io_open)
	# The archive under test, outside the source worktree.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 0
	assert reads == 1

	with zipfile.ZipFile(archive_path) as archive:
		assert archive.read("files/changed.txt") == b"after\n"


def test_build_refuses_changed_internal_link_target(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Ask for a new draft when a selected link's target changes."""
	# Keep the target outside Git selection while leaving it inside the repository.
	(packet_repo / ".git" / "info" / "exclude").write_text("target.txt\n")
	(packet_repo / "target.txt").write_text("first\n")
	(packet_repo / "linked.txt").symlink_to("target.txt")
	assert cli.main(["draft", "chk_example"]) == 0
	(packet_repo / "target.txt").write_text("second\n")
	# The destination that must stay absent after a refusal.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 1

	assert "linked.txt" in capsys.readouterr().err
	assert not archive_path.exists()


def test_build_packs_internal_link_target_bytes(
	packet_repo: Path, tmp_path: Path
) -> None:
	"""Write an internal link's target contents as the archive member."""
	(packet_repo / "target.txt").write_text("target contents\n")
	(packet_repo / "linked.txt").symlink_to("target.txt")
	assert cli.main(["draft", "chk_example"]) == 0
	# The archive under test, outside the source worktree.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 0

	with zipfile.ZipFile(archive_path) as archive:
		assert archive.read("files/linked.txt") == b"target contents\n"


def test_build_uses_downloads_when_output_is_omitted(
	packet_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Write the default archive in the current user's Downloads folder."""
	monkeypatch.setattr(Path, "home", lambda: tmp_path)
	assert cli.main(["draft", "chk_example"]) == 0

	assert cli.main(["build", "chk_example"]) == 0

	assert (tmp_path / "Downloads" / "repo-review-chk_example.zip").exists()


def test_build_json_reports_the_packet(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Return the absolute packet path in the shared JSON envelope."""
	assert cli.main(["draft", "chk_example"]) == 0
	capsys.readouterr()
	# The explicit destination named in the JSON result.
	archive_path = tmp_path / "packet.zip"

	assert (
		cli.main(["build", "chk_example", "--output", str(archive_path), "--json"]) == 0
	)

	assert json.loads(capsys.readouterr().out) == {
		"ok": True,
		"data": {"packet": str(archive_path)},
	}


def test_build_json_reports_stale_paths(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Name a changed selected file in the JSON refusal."""
	assert cli.main(["draft", "chk_example"]) == 0
	capsys.readouterr()
	(packet_repo / "changed.txt").write_text("later\n")

	assert (
		cli.main(
			["build", "chk_example", "--output", str(tmp_path / "packet.zip"), "--json"]
		)
		== 1
	)

	# The JSON error envelope for the out-of-date refusal.
	error = json.loads(capsys.readouterr().out)["error"]
	assert error["code"] == "check-failed"
	assert error["data"]["paths"] == ["changed.txt"]


def test_build_refuses_edited_fingerprint(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Name the manifest when its fingerprint no longer matches the draft."""
	assert cli.main(["draft", "chk_example"]) == 0
	# The draft record whose fingerprint is edited by hand.
	manifest_path = cli._brief_path(packet_repo, "chk_example").with_name(
		"manifest.json"
	)
	# The stored manifest fields, changed and written back below.
	manifest = json.loads(manifest_path.read_text())
	manifest["fingerprint"] = "changed-by-hand"
	manifest_path.write_text(json.dumps(manifest))

	assert (
		cli.main(["build", "chk_example", "--output", str(tmp_path / "packet.zip")])
		== 1
	)

	assert "manifest.json" in capsys.readouterr().err


def test_build_refuses_deleted_selected_file(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Name a selected file removed after drafting."""
	assert cli.main(["draft", "chk_example"]) == 0
	(packet_repo / "new.txt").unlink()

	assert (
		cli.main(["build", "chk_example", "--output", str(tmp_path / "packet.zip")])
		== 1
	)

	assert "new.txt" in capsys.readouterr().err


def test_build_includes_deleted_base_file_in_patch(
	packet_repo: Path, tmp_path: Path
) -> None:
	"""Keep a deletion from the base commit in the review patch."""
	(packet_repo / "README.md").unlink()
	assert cli.main(["draft", "chk_example"]) == 0
	# The explicit destination for the archive under test.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 0

	with zipfile.ZipFile(archive_path) as archive:
		assert "a/README.md" in archive.read("changes.diff").decode()


def test_build_includes_renamed_base_file_in_patch(
	packet_repo: Path, tmp_path: Path
) -> None:
	"""Keep both names of a renamed base file in the review patch."""
	subprocess.run(
		["git", "mv", "README.md", "renamed.md"],
		cwd=packet_repo,
		check=True,
		capture_output=True,
	)
	assert cli.main(["draft", "chk_example"]) == 0
	# The explicit destination for the archive under test.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 0

	with zipfile.ZipFile(archive_path) as archive:
		# The patch that must record the rename.
		patch = archive.read("changes.diff").decode()
		assert "rename from README.md" in patch
		assert "rename to renamed.md" in patch
		assert archive.read("files/renamed.md") == b"Repository\n"


def test_build_reports_missing_draft(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Explain how to create a draft before building."""
	assert (
		cli.main(["build", "chk_example", "--output", str(tmp_path / "packet.zip")])
		== 3
	)

	assert "No draft found. Run review-packet draft first." in capsys.readouterr().err


def test_build_refuses_changed_selected_file(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Name a file changed after draft and leave the destination absent."""
	assert cli.main(["draft", "chk_example"]) == 0
	(packet_repo / "changed.txt").write_text("later\n")
	# The destination that must stay absent after a refusal.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 1

	assert "changed.txt" in capsys.readouterr().err
	assert not archive_path.exists()


def test_build_refuses_changed_git_selection(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Name a new changed file that was absent when the draft was made."""
	assert cli.main(["draft", "chk_example"]) == 0
	(packet_repo / "later.txt").write_text("later\n")
	# The destination that must stay absent after a refusal.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 1

	assert "later.txt" in capsys.readouterr().err
	assert not archive_path.exists()


def test_build_scans_edited_brief(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Refuse a credential added to the brief without printing its value."""
	assert cli.main(["draft", "chk_example"]) == 0
	# The draft brief the author edits before building.
	brief = cli._brief_path(packet_repo, "chk_example")
	# An invented access key, built from parts so this file passes the scan.
	secret = "AKIA" + "1234567890123456"
	brief.write_text(brief.read_text() + f"\n{secret}\n")
	# The destination that must stay absent after a refusal.
	archive_path = tmp_path / "packet.zip"

	assert cli.main(["build", "chk_example", "--output", str(archive_path)]) == 1

	# The refusal text, which must name the file but not the value.
	error = capsys.readouterr().err
	assert "REVIEW.md" in error
	assert secret not in error
	assert not archive_path.exists()


def test_build_json_refuses_edited_brief(
	packet_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Name an unsafe brief in JSON without exposing its credential."""
	assert cli.main(["draft", "chk_example"]) == 0
	capsys.readouterr()
	# The draft brief the author edits before building.
	brief = cli._brief_path(packet_repo, "chk_example")
	# An invented access key, built from parts so this file passes the scan.
	secret = "AKIA" + "1234567890123456"
	brief.write_text(brief.read_text() + f"\n{secret}\n")
	# The destination that must stay absent after a refusal.
	archive_path = tmp_path / "packet.zip"

	assert (
		cli.main(["build", "chk_example", "--output", str(archive_path), "--json"]) == 1
	)

	# The JSON error must identify the brief without printing the matched value.
	output = capsys.readouterr().out
	result = json.loads(output)
	assert result["ok"] is False
	assert result["error"]["code"] == "check-failed"
	assert any(
		finding["path"] == "REVIEW.md"
		for finding in result["error"]["data"]["findings"]
	)
	assert secret not in output
	assert not archive_path.exists()
