"""Check direct Python import lookup in invented repository files."""

from pathlib import Path

from review_packet.imports import find_imports


def test_python_relative_and_repository_imports(tmp_path: Path) -> None:
	"""Find local Python modules while leaving external packages out."""
	# The package holding the changed file and the modules it imports.
	package = tmp_path / "src" / "example"
	package.mkdir(parents=True)
	(package / "__init__.py").write_text("")
	(package / "helper.py").write_text("")
	(package / "other.py").write_text("")
	(package / "changed.py").write_text(
		"import json\nfrom . import helper\nfrom example import other\nfrom .missing import value\n"
	)

	# Imports found for the changed file.
	result = find_imports(tmp_path, "src/example/changed.py")

	assert set(result.paths) == {
		"src/example/__init__.py",
		"src/example/helper.py",
		"src/example/other.py",
	}
	assert result.unresolved == (".missing",)


def test_python_package_submodule_import(tmp_path: Path) -> None:
	"""Include a package entry and its named child module."""
	# A package containing the changed file and imported submodule.
	package = tmp_path / "package"
	package.mkdir()
	(package / "__init__.py").write_text("")
	(package / "submodule.py").write_text("")
	(package / "changed.py").write_text("from package import submodule\n")

	# Files imported by the changed source.
	result = find_imports(tmp_path, "package/changed.py")

	assert set(result.paths) == {"package/__init__.py", "package/submodule.py"}


def test_unparseable_python_file_is_unreadable(tmp_path: Path) -> None:
	"""Report an invalid Python file separately from unresolved imports."""
	# The changed file with invalid Python syntax.
	source = tmp_path / "changed.py"
	source.write_text("from package import\n")

	# The import lookup result for the changed file.
	result = find_imports(tmp_path, "changed.py")

	assert result.unresolved == ()
	assert result.unreadable == ("changed.py",)


def test_name_defined_in_package_file_uses_that_file(tmp_path: Path) -> None:
	"""Use the package's __init__.py for a relative name that isn't a submodule."""
	# The package whose __init__.py defines the imported name.
	package = tmp_path / "package"
	package.mkdir()
	(package / "__init__.py").write_text("VALUE = 1\n")
	(package / "changed.py").write_text("from . import VALUE\n")

	# The import lookup result for the changed file.
	result = find_imports(tmp_path, "package/changed.py")

	assert result.paths == ("package/__init__.py",)
	assert result.unresolved == ()


def test_standard_library_name_does_not_select_local_shadow(tmp_path: Path) -> None:
	"""Ignore a standard library import even when a same-name file exists nearby."""
	# A changed package with a file named like a standard library module.
	package = tmp_path / "src" / "app"
	package.mkdir(parents=True)
	(package / "__init__.py").write_text("")
	(package / "types.py").write_text("")
	(package / "changed.py").write_text("from types import SimpleNamespace\n")

	# Imports found for the changed file.
	result = find_imports(tmp_path, "src/app/changed.py")

	assert result.paths == ()
	assert result.unresolved == ()


def test_non_python_folder_does_not_make_external_import_local(tmp_path: Path) -> None:
	"""Ignore an external import when a matching folder holds no Python files."""
	# A folder named like the external package with no Python source.
	folder = tmp_path / "yaml"
	folder.mkdir()
	(folder / "config.yml").write_text("name: example\n")
	(tmp_path / "changed.py").write_text("import yaml\n")

	# Imports found for the changed file.
	result = find_imports(tmp_path, "changed.py")

	assert result.paths == ()
	assert result.unresolved == ()


def test_sibling_package_import_stays_visible_as_unresolved(tmp_path: Path) -> None:
	"""List an import from another repository package without following it."""
	# The package holding the changed file.
	review = tmp_path / "packages" / "review" / "src" / "review"
	# The sibling package it imports, under its own source folder.
	progress = tmp_path / "packages" / "progress" / "src" / "progress"
	review.mkdir(parents=True)
	progress.mkdir(parents=True)
	(progress / "__init__.py").write_text("")
	(review / "changed.py").write_text("import progress\n")

	# Imports found for the changed file.
	result = find_imports(tmp_path, "packages/review/src/review/changed.py")

	assert result.paths == ()
	assert result.unresolved == ("progress",)
