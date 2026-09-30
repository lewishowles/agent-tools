"""Check relative Markdown links and inline path claims against files on disk."""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# The project scanned when --project-dir is not given, resolved when the command runs.
DEFAULT_PROJECT_DIR = Path(".")
# The configuration file read from the project root when --config is not given.
DEFAULT_CONFIG_FILENAME = "doc-paths.config.json"
# Dependency, cache and tool directories that never hold hand-written docs.
MARKDOWN_SCAN_IGNORE_DIRS = frozenset(
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

# Inline code starting with one of these folders is checked from the project root.
DEFAULT_REPO_PATH_PREFIXES = (
    "scripts/",
    "docs/",
    "tests/",
    "templates/",
    "hooks/",
    "adapters/",
)

# The target of a Markdown link, skipping empty and anchor-only targets.
RE_MD_LINK = re.compile(r"\[(?:[^\]]*)\]\(([^)#\s][^)]*)\)")
# A single-backtick code span on one line.
RE_INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
# A backtick code fence of any length, closed by a fence of the same length.
RE_CODE_FENCE = re.compile(
    r"(?P<fence>`{3,})(?P<language>[^\n`]*)\n(?P<body>.*?)(?P=fence)",
    re.DOTALL,
)
# A doc-paths marker comment that skips path checks on its line.
RE_CLAIM_MARKER = re.compile(r"<!--\s*doc-paths:\s*(planned|historical)\s*-->")
# A path followed by :line or :line:column, as editors and stack traces write it.
RE_SOURCE_LOCATION = re.compile(r"^(?P<path>.+?):\d+(?::\d+)?$")
# Marker values for lines that name files which do not exist yet or no longer exist.
NON_CURRENT_MARKERS = frozenset({"historical", "planned"})


@dataclass
class Issue:
    """Record one missing Markdown path claim.

    Attributes:
        file: Path of the Markdown file, relative to the project directory, or
            absolute when the file is outside it.
        claim: Path string as written in the source.
        kind: Issue kind, currently ``missing_path``.
    """

    file: str
    claim: str
    kind: str


def collect_files(
    project_dir: Path,
    ignore_dirs: frozenset[str] = MARKDOWN_SCAN_IGNORE_DIRS,
) -> list[Path]:
    """Return the project's Markdown files, sorted, skipping ignored directories."""
    project_dir = project_dir.resolve()

    return sorted(
        path
        for path in project_dir.rglob("*.md")
        if not any(part in ignore_dirs for part in path.relative_to(project_dir).parts)
    )


def strip_fences(text: str) -> str:
    """Remove fenced code blocks before scanning Markdown claims."""
    return RE_CODE_FENCE.sub("", text)


def extract_markers(line: str) -> frozenset[str]:
    """Return Markdown claim markers declared on one source line."""
    return frozenset(RE_CLAIM_MARKER.findall(line))


def is_non_current(markers: frozenset[str]) -> bool:
    """Return whether a line describes planned or historical repository state."""
    return bool(markers & NON_CURRENT_MARKERS)


def normalise_path_reference(path: str) -> str:
    """Remove an optional source line and column suffix from a path."""
    match = RE_SOURCE_LOCATION.fullmatch(path)
    if match is None:
        return path

    return match.group("path")


def resolve_claim_matches(path: Path) -> list[Path]:
    """Return the files a path or glob points at; an empty list means it is missing."""
    path_text = str(path)
    if not glob.has_magic(path_text):
        return [path] if path.exists() else []

    return sorted(Path(match) for match in glob.glob(path_text, recursive=True))


def extract_link_claims(text: str, source_file: Path) -> list[tuple[str, Path]]:
    """Extract relative Markdown link claims and their resolved paths.

    Args:
        text: Markdown source with fences already stripped.
        source_file: Absolute path of the file being scanned, used to resolve
            relative targets.

    Returns:
        Pairs containing each claimed target and its resolved path.
    """
    claims = []
    for line in text.splitlines():
        if is_non_current(extract_markers(line)):
            continue

        for match in RE_MD_LINK.finditer(line):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "#", "mailto:", "/")):
                continue
            path_target = target.split("#", maxsplit=1)[0]
            path_target = normalise_path_reference(path_target)
            resolved = (source_file.parent / path_target).resolve()
            claims.append((target, resolved))
    return claims


def extract_inline_path_claims(
    text: str,
    project_dir: Path,
    prefixes: tuple[str, ...],
) -> list[tuple[str, Path]]:
    """Extract current inline-code path claims and their resolved paths.

    Inline claims may include command arguments; only the path token is resolved.

    Args:
        text: Markdown source to scan.
        project_dir: Project directory used to resolve repo-root-relative paths.
        prefixes: Prefixes that identify inline-code path claims.

    Returns:
        Pairs containing each claimed path and its resolved path.
    """
    claims = []
    for line in strip_fences(text).splitlines():
        markers = extract_markers(line)
        if is_non_current(markers):
            continue

        for match in RE_INLINE_CODE.finditer(line):
            code = match.group(1).strip()
            if not code.startswith(prefixes):
                continue
            claim = code.split()[0].rstrip("/")
            if "<" in claim:
                continue
            path_part = normalise_path_reference(claim)
            resolved = project_dir / path_part
            claims.append((claim, resolved))
    return claims


def _load_config(config_path: Path) -> dict[str, object]:
    """Read the optional JSON configuration file.

    Return an empty mapping when the file is absent, and raise ``ValueError`` when
    it is not valid JSON or not a JSON object.
    """
    if not config_path.exists():
        return {}

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in doc-paths configuration: {error}") from error

    if not isinstance(config, dict):
        # Every configuration problem raises ValueError, so the command
        # reports them all the same way.
        raise ValueError("doc-paths configuration must contain a JSON object.")  # noqa: TRY004

    return config


def _load_config_strings(config_path: Path, key: str) -> list[str]:
    """Read one configured key as a list of strings.

    Return an empty list when the key is absent and raise ``ValueError`` when its
    value is not a list of strings.
    """
    config = _load_config(config_path)
    values = config.get(key, [])
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise ValueError(
            f"doc-paths configuration '{key}' must be an array of strings."
        )

    return values


def load_path_prefixes(config_path: Path) -> tuple[str, ...]:
    """Load optional project-specific inline-code path prefixes."""
    extra_prefixes = _load_config_strings(config_path, "extraPathPrefixes")
    return DEFAULT_REPO_PATH_PREFIXES + tuple(extra_prefixes)


def load_ignore_dirs(config_path: Path) -> frozenset[str]:
    """Load optional project-specific Markdown scan ignore directories."""
    extra_ignore_dirs = _load_config_strings(config_path, "extraIgnoreDirs")
    return MARKDOWN_SCAN_IGNORE_DIRS | frozenset(extra_ignore_dirs)


def check_paths(
    project_dir: Path,
    prefixes: tuple[str, ...] = DEFAULT_REPO_PATH_PREFIXES,
    ignore_dirs: frozenset[str] = MARKDOWN_SCAN_IGNORE_DIRS,
    files: list[Path] | None = None,
) -> list[Issue]:
    """Find missing path claims in the given Markdown files or the whole project.

    When ``files`` is None, every Markdown file under ``project_dir`` is scanned
    and ``ignore_dirs`` applies. Files passed explicitly are always checked.
    """
    project_dir = project_dir.resolve()
    if files is None:
        files = collect_files(project_dir, ignore_dirs)
    issues = []

    for md_file in files:
        text = strip_fences(md_file.read_text(encoding="utf-8"))
        try:
            relative_file = str(md_file.relative_to(project_dir))
        except ValueError:
            relative_file = str(md_file)

        link_claims = extract_link_claims(text, md_file)
        code_claims = extract_inline_path_claims(text, project_dir, prefixes)

        for claim, resolved in link_claims + code_claims:
            if not resolve_claim_matches(resolved):
                issues.append(
                    Issue(file=relative_file, claim=claim, kind="missing_path")
                )

    return issues


def _print_issues(issues: list[Issue]) -> None:
    """Print one line per missing path."""
    for issue in issues:
        print(f"{issue.file}: missing path '{issue.claim}'")


def main(arguments: list[str] | None = None) -> None:
    """Run the doc-paths command.

    Exit with status 1 when any path is missing and 2 for invalid arguments or
    configuration.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        metavar="MARKDOWN_FILE",
        help="Markdown files to check (default: all Markdown files under --project-dir).",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=DEFAULT_PROJECT_DIR,
        help="Project directory to inspect.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Path to doc-paths configuration. Defaults to "
            f"<project-dir>/{DEFAULT_CONFIG_FILENAME}."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output findings as JSON",
    )
    args = parser.parse_args(arguments)
    project_dir = args.project_dir.resolve()
    config_path = args.config or project_dir / DEFAULT_CONFIG_FILENAME
    files = [file.resolve() for file in args.files]
    for file in files:
        if not file.is_file() or file.suffix.lower() != ".md":
            parser.error(f"Expected an existing Markdown file: {file}")

    try:
        path_prefixes = load_path_prefixes(config_path)
        ignore_dirs = load_ignore_dirs(config_path)
    except ValueError as error:
        parser.error(str(error))

    issues = check_paths(project_dir, path_prefixes, ignore_dirs, files or None)

    if args.json:
        print(json.dumps({"issues": [asdict(issue) for issue in issues]}, indent=2))
    else:
        _print_issues(issues)
        if issues:
            print()
            print(f"{len(issues)} issue(s) found")

    if issues:
        sys.exit(1)


if __name__ == "__main__":
    main()
