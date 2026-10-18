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
Schema version        4
Database writable     True
Directory writable    True
Integrity             ok
```

## Recording friction

```bash
friction add rule-ignored "skipped the review gate before committing" --key review-gate
```

```
Logged: rule-ignored — skipped the review gate before committing
```

Categories are fixed: `check-fail`, `missing-guidance`, `rule-ignored`, `token-waste`, `tool-error`, `tool-misuse`, `wrong-approach`. `tool-error` is written automatically by the Claude tool-failure hook below; the rest are written by an agent following its own logging instructions. `check-fail` has no automatic writer in this package: it exists so `friction import` can bring in historical rows from a retired check-failure hook.

`--key` is optional. Use a short, lowercase kebab-case name for the repeated mistake, with letters and digits in words separated by single hyphens. Entries without a key remain valid.

When you add a key that no entry uses yet, `friction` lists up to three existing keys that look similar, so you can see whether the mistake already has a name:

```bash
friction add rule-ignored "committed without running the review" --key review-gates
```

```
Logged: rule-ignored — committed without running the review
Similar keys: review-gate
```

Keys from the same category come first, then keys from other categories. The entry is stored with the key you gave either way. In `--json` output, `suggested_keys` is always a list; it is empty when the key is already in use, nothing is close, or no key was given.

To see the keys already in use, run:

```bash
friction keys
```

This lists each key and category with its entry count, most used first. Entries that a resolution hides from `friction summary` still count. Add `--json` to get rows with `key`, `category`, and `count`.

## Reviewing friction

```bash
friction summary
```

```
2  rule-ignored  review-gate  (latest: skipped the review gate before committing)
  folders: /Users/you/project-a, /Users/you/project-b
1  wrong-approach  (latest: tried a manual retry loop instead of fixing the root cause)
  folders: /Users/you/project-a
```

Rows sort by count, highest first. Keyed entries group by category and key across folders, even when their details differ. Unkeyed entries group by category and exact detail. The exception is unkeyed `tool-error` entries, which group by command family and error class. For Bash, the family is the command's program name after leading directory changes, assignments, and common wrappers; for other tools, it is the tool name. Error classes use the exit code or the first sentence, with numbers shown as `N` and paths as `PATH`. Missing values group under `unknown`. Each JSON row has `count`, `category`, `key` (null when absent), `example` (the latest detail), and `folders` (sorted distinct working directories). Unkeyed tool-error rows also have `command_family` and `error_class`. `check-fail` and `tool-error` are excluded by default (`--include-check-fails`, `--include-tool-errors` bring them back), and `--category <name>` filters to one or more categories (repeat the flag to select several).

```bash
friction list --since 2026-09-01 --until 2026-09-04
```

```
ID  Timestamp                  Category        Key          CWD                   Detail
1   2026-09-02T14:07:00+00:00  rule-ignored    review-gate  /Users/you/project-a  skipped the review gate before committing
2   2026-09-03T09:41:00+00:00  wrong-approach               /Users/you/project-a  tried a manual retry loop instead of fixing the root cause
```

`friction list` returns individual events, oldest first, rather than the grouped counts `summary` shows. The text table includes each event's ID and key; an entry without a key has an empty key cell. It takes the same `--category`, `--include-check-fails`, and `--include-tool-errors` flags as `summary`, and adds `--since YYYY-MM-DD` and `--until YYYY-MM-DD`, both inclusive of the named day. Unlike `summary`, it also lists events that a resolution has since hidden. Add `--json` to get one object per event with every column: `id`, `timestamp_utc`, `category`, `cwd`, `detail`, `key` (null when absent), `source`, `tool_name`, `discriminator`, and `error`.

## Adding keys to existing entries

Export the entries, then prepare a JSON object whose property names are entry IDs and whose values are the keys you want to assign:

```bash
friction list --include-check-fails --include-tool-errors --json > /tmp/friction-entries.json
```

```json
{"123": "review-gate", "124": "zsh-status-variable"}
```

Save the mapping to a file, then apply it:

```bash
friction apply-keys /tmp/friction-keys.json
```

```
Keys changed  2
```

`apply-keys` sets or replaces keys without changing entry details. It checks the entire file before writing. If the JSON is malformed, an ID is unknown, or a key does not follow the kebab-case rule, it reports every detected problem and leaves all entries unchanged. Re-applying the same mapping reports zero changed keys. Add `--json` to get `{"ok": true, "data": {"changed": 2}}` on success.

## Resolving a recurring pattern

```bash
friction resolve rule-ignored --key review-gate --reference "src/rules/global-rules.md#L42"
```

```
Resolved: rule-ignored — review-gate
```

This records a resolution without adding a new event. Pass either `--key KEY` to hide entries with that category and key across folders, or an exact detail as the second argument to hide entries with that category and detail. For an unkeyed row outside `tool-error`, use its `example` as the detail. A key resolution matches entries by key only, so an unkeyed entry whose detail equals the key text stays in the summary. `friction summary` hides matching entries logged at or before the resolution timestamp; `friction list` still shows them for review and key mapping. Later recurrences appear in the summary again, so reviewers can see whether the resolution held. JSON resolution output includes `target` as `key` or `detail`.

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
