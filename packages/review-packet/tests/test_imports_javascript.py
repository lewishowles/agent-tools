"""Check direct JavaScript import lookup in invented repository files."""

from pathlib import Path

from review_packet.imports import find_imports


def test_javascript_relative_alias_and_export_imports(tmp_path: Path) -> None:
	"""Find one-level JavaScript imports through relative paths and aliases."""
	# The package holding the changed file, the alias and the imported modules.
	package = tmp_path / "app"
	(package / "src").mkdir(parents=True)
	(package / "package.json").write_text('{"imports": {"#src/*": "./src/*.ts"}}')
	(package / "src" / "helper.ts").write_text("")
	(package / "src" / "other.ts").write_text("")
	(package / "src" / "changed.ts").write_text(
		'import "./helper";\nexport { value } from "#src/other";\n'
		'const x = require("./missing");\nimport external from "vendor";\n'
	)

	# Imports found for the changed file.
	result = find_imports(tmp_path, "app/src/changed.ts")

	assert set(result.paths) == {"app/src/helper.ts", "app/src/other.ts"}
	assert result.unresolved == ("./missing",)


def test_javascript_multiline_import(tmp_path: Path) -> None:
	"""Find a local file imported after a multiline list of names."""
	# The changed source file and its imported module.
	source = tmp_path / "changed.ts"
	(tmp_path / "helper.ts").write_text("")
	source.write_text('import {\n\ta,\n\tb,\n} from "./helper";\n')

	# Files imported by the changed source.
	result = find_imports(tmp_path, "changed.ts")

	assert result.paths == ("helper.ts",)


def test_malformed_package_imports_leave_alias_unresolved(tmp_path: Path) -> None:
	"""Continue when the nearest package manifest cannot supply aliases."""
	# The package containing the changed file.
	package = tmp_path / "app"
	package.mkdir()
	(package / "changed.ts").write_text('import "#src/helper";\n')
	# The package manifest, rewritten with each malformed value below.
	manifest = package / "package.json"

	for contents in ('{"imports":', '{"imports": []}'):
		manifest.write_text(contents)
		# The import lookup result for this manifest value.
		result = find_imports(tmp_path, "app/changed.ts")

		assert result.unresolved == ("#src/helper",)


def test_parent_import_and_outside_path(tmp_path: Path) -> None:
	"""Normalise parent imports and list a path that climbs outside the root."""
	# A changed source file with one parent import and one outside import.
	source_folder = tmp_path / "app" / "src"
	source_folder.mkdir(parents=True)
	(tmp_path / "app" / "helper.ts").write_text("")
	(source_folder / "changed.ts").write_text(
		'import "../helper"\nimport "../../../outside"\n'
	)

	# Imports found for the changed file.
	result = find_imports(tmp_path, "app/src/changed.ts")

	assert result.paths == ("app/helper.ts",)
	assert result.unresolved == ("../../../outside",)


def test_import_that_climbs_to_filesystem_root_is_unresolved(tmp_path: Path) -> None:
	"""List an import that reaches the filesystem root as unresolved."""
	# The folder containing the changed source file.
	source_folder = tmp_path / "app" / "src"
	source_folder.mkdir(parents=True)
	# A relative import with enough parent steps to reach the filesystem root.
	root_import = "/".join(".." for _ in source_folder.parts[1:])
	(source_folder / "changed.ts").write_text(f'import "{root_import}"\n')

	# Imports found for the changed file.
	result = find_imports(tmp_path, "app/src/changed.ts")

	assert result.paths == ()
	assert result.unresolved == (root_import,)


def test_semicolon_free_imports_do_not_swallow_each_other(tmp_path: Path) -> None:
	"""Find side-effect imports followed by another import without semicolons."""
	# A source file containing adjacent imports and an export statement.
	(tmp_path / "side.ts").write_text("")
	(tmp_path / "other.ts").write_text("")
	(tmp_path / "changed.ts").write_text(
		'import "./side"\nexport const value = 1\nimport other from "./other"\n'
	)

	# Imports found for the changed file.
	result = find_imports(tmp_path, "changed.ts")

	assert result.paths == ("other.ts", "side.ts")
	assert result.unresolved == ()


def test_dotted_target_uses_full_name(tmp_path: Path) -> None:
	"""Keep a dotted module name when adding a source suffix."""
	# A dotted module and a shorter module that must not be selected.
	(tmp_path / "user.service.ts").write_text("")
	(tmp_path / "user.ts").write_text("")
	(tmp_path / "changed.ts").write_text('import "./user.service"\n')

	# Imports found for the changed file.
	result = find_imports(tmp_path, "changed.ts")

	assert result.paths == ("user.service.ts",)


def test_commented_import_is_ignored(tmp_path: Path) -> None:
	"""Skip an import declaration that has been commented out."""
	# A source file with a commented import declaration.
	(tmp_path / "changed.ts").write_text('// import gone from "./gone"\n')

	# Imports found for the changed file.
	result = find_imports(tmp_path, "changed.ts")

	assert result.unresolved == ()


def test_alias_target_with_parent_path(tmp_path: Path) -> None:
	"""Find an alias target that moves up to a parent folder."""
	# The package manifest and an imported file above its own folder.
	package = tmp_path / "app"
	package.mkdir()
	(tmp_path / "shared.ts").write_text("")
	(package / "package.json").write_text('{"imports": {"#shared": "../shared.ts"}}')
	(package / "changed.ts").write_text('import "#shared"\n')

	# Files imported by the changed source.
	result = find_imports(tmp_path, "app/changed.ts")

	assert result.paths == ("shared.ts",)
	assert result.unresolved == ()


def test_asset_imports_are_skipped(tmp_path: Path) -> None:
	"""Leave imported images and fonts out of the files and missing imports."""
	# A source file with binary assets and allowed exact-name imports.
	(tmp_path / "logo.png").write_bytes(b"image")
	(tmp_path / "font.woff2").write_bytes(b"font")
	(tmp_path / "panel.vue").write_text("")
	(tmp_path / "theme.css").write_text("")
	(tmp_path / "changed.ts").write_text(
		'import "./logo.png"\nimport "./font.woff2"\n'
		'import "./panel.vue"\nimport "./theme.css"\n'
	)

	# Files imported by the changed source.
	result = find_imports(tmp_path, "changed.ts")

	assert result.paths == ("panel.vue", "theme.css")
	assert result.unresolved == ()
