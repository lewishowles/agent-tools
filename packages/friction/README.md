# friction

`friction` is a local command-line tool for recording and reviewing agent friction: moments where an AI coding agent ignored a rule, took the wrong approach, wasted tokens, misused a tool, or lacked guidance. Events live in a SQLite database.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Getting started

From the `dev-tools` repository root, install the local package into your uv tool environment:

```bash
uv tool install --reinstall --from packages/friction friction
```

This puts `friction` on your `PATH` without publishing the package. Events are stored in `~/.agents/friction.db` by default; override the path with `--database <path>` or the `FRICTION_DATABASE` environment variable.

Check the install worked:

```bash
friction doctor
```

```
Database             /Users/you/.agents/friction.db
Schema version        2
Database writable     True
Directory writable    True
Integrity             ok
```

## Recording friction

```bash
friction add rule-ignored "skipped the review gate before committing"
```

```
Logged: rule-ignored — skipped the review gate before committing
```

Categories are fixed: `check-fail`, `missing-guidance`, `rule-ignored`, `token-waste`, `tool-error`, `tool-misuse`, `wrong-approach`. `tool-error` is written automatically by the Claude tool-failure hook below; the rest are written by an agent following its own logging instructions. `check-fail` has no automatic writer in this package: it exists so `friction import` can bring in historical rows from a retired check-failure hook.

## Reviewing friction

```bash
friction summary
```

```
2  rule-ignored    /Users/you/project-a  skipped the review gate before committing
1  wrong-approach  /Users/you/project-a  tried a manual retry loop instead of fixing the root cause
```

Rows group by exact category, working directory, and detail, sorted by count. `check-fail` and `tool-error` are excluded by default (`--include-check-fails`, `--include-tool-errors` bring them back), and `--category <name>` filters to one or more categories (repeat the flag to select several).

```bash
friction list --since 2026-09-01 --until 2026-09-04
```

```
2026-09-02T14:07:00+00:00  rule-ignored    /Users/you/project-a  skipped the review gate before committing
2026-09-03T09:41:00+00:00  wrong-approach  /Users/you/project-a  tried a manual retry loop instead of fixing the root cause
```

`friction list` returns individual events, oldest first, rather than the grouped counts `summary` shows. It takes the same `--category`, `--include-check-fails`, and `--include-tool-errors` flags as `summary`, and adds `--since YYYY-MM-DD` and `--until YYYY-MM-DD`, both inclusive of the named day. Unlike `summary`, it also lists events that a resolution has since hidden. Add `--json` to get one object per event with every column: `timestamp_utc`, `category`, `cwd`, `detail`, `source`, `tool_name`, `discriminator`, and `error`.

## Resolving a recurring pattern

```bash
friction resolve rule-ignored "skipped the review gate before committing" --reference "src/rules/global-rules.md#L42"
```

```
Resolved: rule-ignored — skipped the review gate before committing
```

This records a resolution without adding a new event. `friction summary` hides events with that exact category and detail at or before the resolution timestamp. Later recurrences appear again, so reviewers can see whether the resolution held.

## Migrating an old TSV log

```bash
friction import ~/.claude/logs/friction.log
```

Reads the current four-column format, the legacy `check-fail` format, and `RESOLVED` marker rows, and reports how many rows were imported, skipped as duplicates, or rejected (with a reason for each rejected line). Re-running `import` on the same file is safe: already-imported rows are recognised by their source file and line number and skipped rather than duplicated. Add `--dry-run` to see the counts without writing anything. Source files are never modified or deleted.

## Claude's automatic tool-failure hook

```bash
friction hook claude-tool-failure
```

Reads Claude's `PostToolUseFailure` JSON from stdin and records one `tool-error` event with the tool name, a short discriminator (the command, file path, or similar field from the tool input), and the error message, each sanitised and truncated to 300 characters. It never raises and always exits `0`, even on malformed or unreadable input, so it can never block Claude's tool flow. There's no Codex equivalent yet: Codex has no automatic tool-failure event to hook into, so Codex-originated friction still goes through `friction add`.

## Flags every command accepts

- `--json`: write a machine-readable result instead of the human-readable output shown above
- `--database <path>`: use `<path>` instead of `$FRICTION_DATABASE` or `~/.agents/friction.db`
