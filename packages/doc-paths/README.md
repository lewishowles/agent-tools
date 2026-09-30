# Doc paths

`doc-paths` checks whether relative Markdown links and path-like inline code point to existing files. It skips fenced code, URLs, anchors, root-absolute links, and lines marked `<!-- doc-paths: planned -->` or `<!-- doc-paths: historical -->`.

Install it from this workspace:

```sh
uv tool install --from packages/doc-paths doc-paths
```

Run `doc-paths --project-dir .` to scan every Markdown file in a project. Pass file paths to check only those files, for example `doc-paths --project-dir . README.md docs/setup.md`. File paths are relative to the current directory; relative Markdown links resolve from their source file, while inline code paths resolve from `--project-dir`.

Use `--json` for `{"issues": [...]}` output. The command exits with 0 when all paths exist, 1 when paths are missing, and 2 for invalid arguments or configuration.

Add `doc-paths.config.json` at the project root to extend the built-in inline-code path prefixes or ignored scan directories:

```json
{
  "extraPathPrefixes": ["guides/"],
  "extraIgnoreDirs": ["generated"]
}
```

Ignored directories apply to full-project scans. Explicit file paths are always checked. Use `--config PATH` to read a different configuration file.
