"""Check import lookup dispatch by source language."""

from pathlib import Path

from review_packet.imports import find_imports


def test_non_source_file_has_no_imports(tmp_path: Path) -> None:
	"""Skip files whose source language is not supported."""
	# A changed file that is not Python or JavaScript source.
	source = tmp_path / "notes.md"
	source.write_text('import "./missing"')

	# Imports found for the unsupported file.
	result = find_imports(tmp_path, "notes.md")

	assert result.paths == ()
	assert result.unresolved == ()
