import json
from pathlib import Path

import pytest
from doc_paths.cli import (
    DEFAULT_REPO_PATH_PREFIXES,
    MARKDOWN_SCAN_IGNORE_DIRS,
    Issue,
    check_paths,
    collect_files,
    load_ignore_dirs,
    load_path_prefixes,
    main,
)


def test_default_path_prefixes_are_generic() -> None:
    """The built-in path prefixes stay limited to folders common across projects."""
    assert DEFAULT_REPO_PATH_PREFIXES == (
        "scripts/",
        "docs/",
        "tests/",
        "templates/",
        "hooks/",
        "adapters/",
    )


def test_default_scan_ignore_dirs_are_unambiguous() -> None:
    """The built-in ignored directories only name dependency, cache and tool folders."""
    assert MARKDOWN_SCAN_IGNORE_DIRS == frozenset(
        {
            ".cache",
            ".git",
            ".mypy_cache",
            ".next",
            ".nuxt",
            ".pytest_cache",
            ".ruff_cache",
            ".tox",
            ".venv",
            "__pycache__",
            "coverage",
            "htmlcov",
            "node_modules",
            "vendor",
        }
    )


@pytest.mark.parametrize("prefix", DEFAULT_REPO_PATH_PREFIXES)
def test_default_path_prefix_is_recognised(tmp_path: Path, prefix: str) -> None:
    """Inline code under each built-in prefix is checked against the project."""
    target = tmp_path / prefix / "claimed.txt"
    target.parent.mkdir(parents=True)
    target.write_text("present", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        f"Use `{prefix}claimed.txt`.\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == []


def test_extra_path_prefixes_are_merged_and_recognised(tmp_path: Path) -> None:
    """Configured prefixes are added to the built-in ones and checked."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(
        json.dumps({"extraPathPrefixes": ["guides/"]}),
        encoding="utf-8",
    )
    target = tmp_path / "guides" / "claimed.txt"
    target.parent.mkdir()
    target.write_text("present", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "See `guides/claimed.txt`.\n",
        encoding="utf-8",
    )

    prefixes = load_path_prefixes(config_path)

    assert prefixes == DEFAULT_REPO_PATH_PREFIXES + ("guides/",)
    assert check_paths(tmp_path, prefixes) == []


def test_extra_ignore_dirs_are_merged_and_suppress_configured_dirs(
    tmp_path: Path,
    capsys,
) -> None:
    """Configured ignored directories are skipped by both the scan and the command."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(
        json.dumps({"extraIgnoreDirs": ["dist"]}),
        encoding="utf-8",
    )
    dist_file = tmp_path / "dist" / "README.md"
    dist_file.parent.mkdir()
    dist_file.write_text("See `scripts/generated-missing.sh`.\n", encoding="utf-8")
    build_file = tmp_path / "build" / "README.md"
    build_file.parent.mkdir()
    build_file.write_text("# Hand-authored source\n", encoding="utf-8")

    ignore_dirs = load_ignore_dirs(config_path)

    assert ignore_dirs == MARKDOWN_SCAN_IGNORE_DIRS | frozenset({"dist"})
    assert collect_files(tmp_path, ignore_dirs) == [build_file]

    main(
        [
            "--project-dir",
            str(tmp_path),
            "--config",
            str(config_path),
            "--json",
        ]
    )

    assert json.loads(capsys.readouterr().out) == {"issues": []}


def test_config_override_is_used_by_cli(tmp_path: Path, capsys) -> None:
    """The command reads the configuration file named by --config."""
    config_path = tmp_path / "custom-config.json"
    config_path.write_text(
        json.dumps({"extraPathPrefixes": ["guides/"]}),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "See `guides/missing.md`.\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as error:
        main(
            [
                "--project-dir",
                str(tmp_path),
                "--config",
                str(config_path),
                "--json",
            ]
        )

    assert error.value.code == 1
    assert "guides/missing.md" in capsys.readouterr().out


def test_old_config_filename_is_not_loaded(tmp_path: Path, capsys) -> None:
    """The old config file does not add inline path prefixes."""
    (tmp_path / "markdown-claims.config.json").write_text(
        json.dumps({"extraPathPrefixes": ["guides/"]}),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "See `guides/missing.md`.\n",
        encoding="utf-8",
    )

    main(["--project-dir", str(tmp_path), "--json"])

    assert json.loads(capsys.readouterr().out) == {"issues": []}


def test_repo_wide_scan_includes_root_markdown_and_ignores_noise_dirs(
    tmp_path: Path,
) -> None:
    """A full scan checks root Markdown files and skips dependency folders."""
    (tmp_path / "README.md").write_text(
        "See `scripts/missing.py`.\nRun `./scripts/missing.sh`.\n",
        encoding="utf-8",
    )
    ignored_file = tmp_path / "node_modules" / "ignored.md"
    ignored_file.parent.mkdir()
    ignored_file.write_text(
        "Run `scripts/ignored.sh`.\n",
        encoding="utf-8",
    )

    path_issues = check_paths(tmp_path)

    assert [(issue.file, issue.claim) for issue in path_issues] == [
        ("README.md", "scripts/missing.py"),
    ]


def test_markdown_files_in_dist_and_build_are_scanned_by_default(
    tmp_path: Path,
) -> None:
    """dist and build folders are scanned unless the configuration ignores them."""
    markdown_files = []
    for directory in ("dist", "build"):
        markdown_file = tmp_path / directory / "README.md"
        markdown_file.parent.mkdir()
        markdown_file.write_text("# Hand-authored source\n", encoding="utf-8")
        markdown_files.append(markdown_file)

    assert collect_files(tmp_path) == sorted(markdown_files)


def test_invalid_config_json_raises_clear_error(tmp_path: Path) -> None:
    """A configuration file that is not valid JSON names the problem."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text("{", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="^Invalid JSON in doc-paths configuration:",
    ):
        load_path_prefixes(config_path)


@pytest.mark.parametrize("config", [None, [], "invalid", 1, True])
def test_path_prefix_config_requires_a_json_object(
    tmp_path: Path,
    config: object,
) -> None:
    """Prefix loading rejects a configuration that is not a JSON object."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="^doc-paths configuration must contain a JSON object\\.$",
    ):
        load_path_prefixes(config_path)


def test_non_object_config_is_a_cli_usage_error(tmp_path: Path, capsys) -> None:
    """The command exits with status 2 when the configuration is not an object."""
    (tmp_path / "doc-paths.config.json").write_text("[]", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--project-dir", str(tmp_path)])

    assert error.value.code == 2
    assert "must contain a JSON object" in capsys.readouterr().err


@pytest.mark.parametrize("value", [None, "guides/"])
def test_extra_path_prefixes_must_be_an_array(
    tmp_path: Path,
    value: object,
) -> None:
    """extraPathPrefixes must be a list."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(
        json.dumps({"extraPathPrefixes": value}),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="'extraPathPrefixes' must be an array of strings\\.$",
    ):
        load_path_prefixes(config_path)


@pytest.mark.parametrize("value", [["guides/", 1], [None]])
def test_extra_path_prefixes_must_contain_only_strings(
    tmp_path: Path,
    value: list[object],
) -> None:
    """extraPathPrefixes must hold only strings."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(
        json.dumps({"extraPathPrefixes": value}),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="'extraPathPrefixes' must be an array of strings\\.$",
    ):
        load_path_prefixes(config_path)


@pytest.mark.parametrize("config", [None, [], "invalid", 1, True])
def test_ignore_dirs_config_requires_a_json_object(
    tmp_path: Path,
    config: object,
) -> None:
    """Ignored-directory loading rejects a configuration that is not a JSON object."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="^doc-paths configuration must contain a JSON object\\.$",
    ):
        load_ignore_dirs(config_path)


@pytest.mark.parametrize("value", [None, "dist"])
def test_extra_ignore_dirs_must_be_an_array(
    tmp_path: Path,
    value: object,
) -> None:
    """extraIgnoreDirs must be a list."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(
        json.dumps({"extraIgnoreDirs": value}),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="'extraIgnoreDirs' must be an array of strings\\.$",
    ):
        load_ignore_dirs(config_path)


@pytest.mark.parametrize("value", [["dist", 1], [None]])
def test_extra_ignore_dirs_must_contain_only_strings(
    tmp_path: Path,
    value: list[object],
) -> None:
    """extraIgnoreDirs must hold only strings."""
    config_path = tmp_path / "doc-paths.config.json"
    config_path.write_text(
        json.dumps({"extraIgnoreDirs": value}),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="'extraIgnoreDirs' must be an array of strings\\.$",
    ):
        load_ignore_dirs(config_path)


def test_missing_config_uses_default_prefixes(tmp_path: Path) -> None:
    """Without a configuration file, only the built-in prefixes apply."""
    assert load_path_prefixes(tmp_path / "missing.json") == DEFAULT_REPO_PATH_PREFIXES


def test_missing_config_uses_default_ignore_dirs(tmp_path: Path) -> None:
    """Without a configuration file, only the built-in ignored directories apply."""
    assert load_ignore_dirs(tmp_path / "missing.json") == MARKDOWN_SCAN_IGNORE_DIRS


def test_relative_link_with_fragment_validates_the_path_before_fragment(
    tmp_path: Path,
) -> None:
    """A link with a #heading is checked by its file path alone."""
    manual = tmp_path / "references" / "manual-checks.md"
    manual.parent.mkdir()
    manual.write_text("# Component states\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[Manual](references/manual-checks.md#component-states)\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == []


@pytest.mark.parametrize(
    "reference",
    ("scripts/tool.py:150", "scripts/tool.py:150:12"),
)
def test_existing_path_with_source_location_uses_path_portion(
    tmp_path: Path,
    reference: str,
) -> None:
    """A :line or :line:column suffix is ignored when the file exists."""
    script = tmp_path / "scripts" / "tool.py"
    script.parent.mkdir()
    script.write_text("print('checked')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        f"See `{reference}`.\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == []


def test_missing_current_path_with_source_location_still_fails(tmp_path: Path) -> None:
    """A missing file is still reported when it has a :line suffix."""
    (tmp_path / "README.md").write_text(
        "See `scripts/missing.py:150`.\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == [
        Issue(file="README.md", claim="scripts/missing.py:150", kind="missing_path"),
    ]


@pytest.mark.parametrize("marker", ("planned", "historical"))
def test_explicit_non_current_path_marker_is_ignored(
    tmp_path: Path,
    marker: str,
) -> None:
    """A planned or historical marker skips the path on its line."""
    (tmp_path / "README.md").write_text(
        f"`scripts/not-current.py` <!-- doc-paths: {marker} -->\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == []


def test_non_current_marker_only_applies_to_its_line(tmp_path: Path) -> None:
    """A marker does not skip paths on the following lines."""
    (tmp_path / "README.md").write_text(
        "`scripts/planned.py` <!-- doc-paths: planned -->\n`scripts/missing.py`\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == [
        Issue(file="README.md", claim="scripts/missing.py", kind="missing_path"),
    ]


def test_old_marker_does_not_suppress_a_missing_path(tmp_path: Path) -> None:
    """The old markdown-claims marker no longer skips a path."""
    (tmp_path / "README.md").write_text(
        "`scripts/missing.py` <!-- markdown-claims: planned -->\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == [
        Issue(file="README.md", claim="scripts/missing.py", kind="missing_path"),
    ]


def test_longer_markdown_fence_is_ignored_by_path_checks(tmp_path: Path) -> None:
    """Paths inside a four-backtick fence are not checked."""
    (tmp_path / "README.md").write_text(
        "````markdown\n[Docs](docs/)\n`docs/`\n````\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == []


def test_matched_path_glob_passes_and_unmatched_path_glob_fails(tmp_path: Path) -> None:
    """A glob passes when it matches a file and fails when it matches nothing."""
    script = tmp_path / "scripts" / "tools" / "present.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "Current: `scripts/tools/*.sh`. Missing: `scripts/other/*.sh`.\n",
        encoding="utf-8",
    )

    assert check_paths(tmp_path) == [
        Issue(file="README.md", claim="scripts/other/*.sh", kind="missing_path"),
    ]


def test_selected_files_resolve_links_from_each_file_and_inline_paths_from_project(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """File arguments resolve from the current directory, links from each file and inline paths from --project-dir."""
    selected = tmp_path / "README.md"
    selected.write_text(
        "[Nearby](docs/nearby.md) `scripts/present.sh`\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "nearby.md").write_text("# Nearby\n", encoding="utf-8")
    script = tmp_path / "scripts" / "present.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "other.md").write_text(
        "`scripts/missing.sh`\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(docs)

    main(["--project-dir", str(tmp_path), "../README.md", "nearby.md", "--json"])

    assert json.loads(capsys.readouterr().out) == {"issues": []}


def test_selected_file_in_ignored_directory_is_checked(tmp_path: Path, capsys) -> None:
    """Explicit Markdown files are checked even inside ignored directories."""
    selected = tmp_path / "node_modules" / "README.md"
    selected.parent.mkdir()
    selected.write_text("`scripts/missing.sh`\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--project-dir", str(tmp_path), str(selected), "--json"])

    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "issues": [
            {
                "file": "node_modules/README.md",
                "claim": "scripts/missing.sh",
                "kind": "missing_path",
            }
        ]
    }


def test_selected_file_reports_missing_path_in_plain_text(
    tmp_path: Path, capsys
) -> None:
    """A missing path in a given file prints as plain text and exits with 1."""
    selected = tmp_path / "README.md"
    selected.write_text("[Missing](docs/missing.md)\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--project-dir", str(tmp_path), str(selected)])

    assert error.value.code == 1
    assert capsys.readouterr().out == (
        "README.md: missing path 'docs/missing.md'\n\n1 issue(s) found\n"
    )


@pytest.mark.parametrize("name", ("missing.md", "notes.txt"))
def test_invalid_file_argument_is_a_usage_error(
    tmp_path: Path,
    capsys,
    name: str,
) -> None:
    """A missing or non-Markdown file argument exits with status 2."""
    if name == "notes.txt":
        (tmp_path / name).write_text("# Not Markdown\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--project-dir", str(tmp_path), str(tmp_path / name)])

    assert error.value.code == 2
    assert "Expected an existing Markdown file" in capsys.readouterr().err
