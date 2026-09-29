import subprocess

import pytest

from review_feedback import clipboard


def enable_macos_clipboard(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
	"""Configure one macOS clipboard command as available for a test."""
	monkeypatch.setattr(clipboard.sys, "platform", "darwin")
	monkeypatch.setattr(clipboard.shutil, "which", lambda _: f"/usr/bin/{command}")


@pytest.mark.parametrize(
	("selection", "expected"),
	[
		(
			" 808 +       </p>\n"
			" 809 + \n"
			" 806   line 806\n"
			" 807 - old line\n"
			" 810 ~ changed line\n"
			" 811   \n",
			"      </p>\n\nline 806\nold line\nchanged line\n\n",
		),
		("plain text\n  indented source\n", "plain text\n  indented source\n"),
		(
			" 808 + added\nplain source\n 810 -       removed\n",
			"added\nplain source\n      removed\n",
		),
	],
)
def test_strip_clipboard_gutter(selection: str, expected: str) -> None:
	result = clipboard.strip_clipboard_gutter(selection)

	assert result == expected


def test_read_clipboard_rejects_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(clipboard.sys, "platform", "linux")

	with pytest.raises(clipboard.ClipboardError, match="macOS only"):
		clipboard.read_clipboard()


def test_read_clipboard_reports_missing_pbpaste(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(clipboard.sys, "platform", "darwin")
	monkeypatch.setattr(clipboard.shutil, "which", lambda _: None)

	with pytest.raises(clipboard.ClipboardError, match="pbpaste was not found"):
		clipboard.read_clipboard()


def test_read_clipboard_reports_pbpaste_disappearing_before_run(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	enable_macos_clipboard(monkeypatch, "pbpaste")
	monkeypatch.setattr(
		clipboard.subprocess,
		"run",
		lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
	)

	with pytest.raises(clipboard.ClipboardError, match="pbpaste was not found"):
		clipboard.read_clipboard()


def test_read_clipboard_reports_os_error(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	enable_macos_clipboard(monkeypatch, "pbpaste")
	monkeypatch.setattr(
		clipboard.subprocess,
		"run",
		lambda *args, **kwargs: (_ for _ in ()).throw(OSError("permission denied")),
	)

	with pytest.raises(clipboard.ClipboardError, match="permission denied"):
		clipboard.read_clipboard()


def test_read_clipboard_reports_command_failure(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	enable_macos_clipboard(monkeypatch, "pbpaste")
	monkeypatch.setattr(
		clipboard.subprocess,
		"run",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			subprocess.CalledProcessError(1, "pbpaste")
		),
	)

	with pytest.raises(clipboard.ClipboardError, match="status 1"):
		clipboard.read_clipboard()


def test_read_clipboard_rejects_non_utf8_content(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	enable_macos_clipboard(monkeypatch, "pbpaste")
	monkeypatch.setattr(
		clipboard.subprocess,
		"run",
		lambda *args, **kwargs: subprocess.CompletedProcess(
			args=["pbpaste"], returncode=0, stdout=b"\xff"
		),
	)

	with pytest.raises(clipboard.ClipboardError, match="not readable UTF-8"):
		clipboard.read_clipboard()


def test_read_clipboard_rejects_empty_content(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	enable_macos_clipboard(monkeypatch, "pbpaste")
	monkeypatch.setattr(
		clipboard.subprocess,
		"run",
		lambda *args, **kwargs: subprocess.CompletedProcess(
			args=["pbpaste"], returncode=0, stdout=b""
		),
	)

	with pytest.raises(clipboard.ClipboardError, match="clipboard is empty"):
		clipboard.read_clipboard()


def test_copy_to_clipboard_rejects_non_macos(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(clipboard.sys, "platform", "linux")

	with pytest.raises(clipboard.ClipboardError, match="macOS only"):
		clipboard.copy_to_clipboard("packet")


def test_copy_to_clipboard_reports_missing_pbcopy(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(clipboard.sys, "platform", "darwin")
	monkeypatch.setattr(clipboard.shutil, "which", lambda _: None)

	with pytest.raises(clipboard.ClipboardError, match="pbcopy was not found"):
		clipboard.copy_to_clipboard("packet")


def test_copy_to_clipboard_reports_pbcopy_failure(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	monkeypatch.setattr(clipboard.sys, "platform", "darwin")
	monkeypatch.setattr(clipboard.shutil, "which", lambda _: "/usr/bin/pbcopy")
	monkeypatch.setattr(
		clipboard.subprocess,
		"run",
		lambda *args, **kwargs: (_ for _ in ()).throw(
			subprocess.CalledProcessError(1, "pbcopy")
		),
	)

	with pytest.raises(clipboard.ClipboardError, match="status 1"):
		clipboard.copy_to_clipboard("packet")
