# agent-run

`agent-run` will discover, register, and run project commands while returning
bounded results and complete evidence for failures.

The command shell exposes help, repository identification, named command
registration, direct command execution, and the shared text and JSON output
contract. Targeting and evidence arrive in later chunks.

Install it once as an editable global tool so the `agent-run` on your PATH
tracks this checkout without reinstalling after each change:

```sh
uv tool install --editable packages/agent-run
```

```sh
agent-run --help
agent-run --version
agent-run --json
agent-run init
agent-run repository
agent-run repository --json
```

Use `agent-run --version` to print the installed version. Add `--json` to get
the version as JSON.

Run `agent-run init` once in each clone before using any command that works
with the repository; `--version`, `--help` and `prune` work without it. It
creates a stable ID in the clone-local Git configuration under
`agent-run.repository-id`. The ID is never committed. Linked worktrees share
the main checkout's local configuration and therefore share its ID; each
separate clone needs its own `agent-run init`.

`agent-run repository` finds the Git repository containing the current working
directory and prints its root path and ID.

## Error codes

With `--json`, a failed command reports one of these codes. The number in
brackets is the exit status, which is the same with or without `--json`.

- `not-found` (1): The named command or run does not exist
- `check-failed` (1): The command ran and failed
- `busy` (1): The named command is already running
- `manual` (1): The command needs a human to run it
- `usage` (2): The command arguments are invalid
- `environment` (3): Git, the database, or the local environment could not be used
- `uninitialised` (3): This clone has no agent-run ID yet; run `agent-run init` once
- `internal` (3): agent-run hit an unexpected error

## Register commands

Add a named command from inside a Git repository. The command stores its
argument array and working directory, which defaults to the repository root;
pass `--cwd` to choose another directory inside it and `--timeout` to set the
timeout used by named runs. If you leave out `--timeout`, named runs use the
120-second default. Set `--capability file-list` when the command accepts file
paths appended with `--file` or `--glob`; commands use the `none` capability by
default. Add `--manual` when a command needs a human to run it instead of
agent-run.

Agent-run also marks commands that start Playwright or Cypress as manual-only.
This includes commands run through `npx`, `pnpm exec`, `yarn`, `bunx`, or
`uv run`. Use `agent-run edit NAME --no-manual` when a command has been checked
and is safe to run automatically.

```sh
agent-run add test --timeout 30 -- pytest
agent-run add lint --cwd tools --json -- ruff check
agent-run add lint-changed --capability file-list -- ruff check
agent-run add shell-syntax --capability file-list -- zsh -n
agent-run add browser-check --manual -- npm run browser-check
```

Change a command's working directory, arguments, or timeout. Anything you leave
out stays as it is:

```sh
agent-run edit lint --cwd scripts
agent-run edit lint -- ruff check --fix
agent-run edit lint --timeout 10
agent-run edit lint-changed --capability none
agent-run edit browser-check --no-manual
```

Changing a command's arguments to a Playwright or Cypress runner marks it
manual-only, unless the same edit includes `--no-manual`.

Rename or remove a command:

```sh
agent-run rename old-name new-name
agent-run remove old-name
```

List the commands registered for the current repository in name order:

```sh
agent-run list
agent-run list --json
```

Preview commands detected in the current repository without registering them:

```sh
agent-run detect
agent-run detect --json
```

On its own, `detect` only lists what it finds and saves nothing. It looks only
at supported paths in the repository root, `tests/`, and `scripts/`:

- `package.json`: a command for each script, run with the package manager that
  matches the root lockfile, or npm when there is no lockfile.
- `pyproject.toml`: pytest and ruff checks, for each tool the file configures.
- `Package.swift`: `swift build` and `swift test`.
- `.swift-format`: `swift-format lint --recursive .`.
- Scripts directly under `tests/` and `scripts/`: every `.sh` file runs with
  Bash; executable files without a suffix run directly.

`detect` also lists what it left out, with a reason for each: candidates whose
name an earlier candidate already uses, and files directly under `tests/` or
`scripts/` that are neither `.sh` files nor executable files without a suffix.
An executable with another suffix, such as `tests/check.py`, is left out; add it
with `agent-run add`. An empty skipped list means nothing was left out.

Xcode projects and packages below the root are not detected; add those with
`agent-run add`.

When a package script starts a Playwright or Cypress runner, saving the detected
command marks it manual-only. Detection checks the script body once and does not
follow other package scripts.

Save detected commands by name, or save every one that is not saved yet:

```sh
agent-run detect --add test
agent-run detect --add test --add lint
agent-run detect --all
```

Run `agent-run detect --add` in an interactive terminal to choose from a
checkbox list. New commands start ticked. Commands already saved exactly are
shown as already registered. A command whose name is saved with a different
command or folder can't be ticked; the list shows the `agent-run edit` command
that would replace it. Saving never overwrites an existing command.

Working directories must stay inside the repository. A symlinked directory is
stored as its resolved target.

## Run commands

Run a saved command by name or a direct argument-array command in the
foreground. For direct runs, `--cwd` selects a directory inside the current
repository. Named runs use the saved command's working directory. The effective
timeout is chosen in this order: `--timeout`, the saved command's timeout, then
the 120-second default.

A command marked `--manual` still appears in `list`, but `run` refuses to
execute it and prints the exact command and working directory for a human to
run instead. Direct Playwright and Cypress commands are refused in the same
way, before they start or create a run record.

```sh
agent-run run test
agent-run run --timeout 30 -- pytest
agent-run run --cwd tools --timeout 10 --json -- ruff check
agent-run run lint-changed --file src/main.py --file src/cli.py
agent-run run lint-changed --glob 'src/**/*.py'
```

The command's combined standard output and standard error is written to a
private log file. Text output reports the exit status, duration, run ID, and log
path. JSON output returns the same run details in `data`, including for failed,
timed-out, and interrupted runs. A named command with the `file-list`
capability appends each `--file` path and each `--glob` match to its stored
arguments. `--file` paths are resolved from the current directory. `--glob`
patterns are resolved from the repository root through Git, so ignored files
and tracked files deleted from disk are excluded. Each pattern's matches are
sorted, all targets must be regular files inside the repository, and targets
are reported relative to the command's working directory.

A successful run also shows the last eight non-blank lines of its output, so
you can see what it did, such as how many tests ran. JSON output returns these
lines as `summary` in `data`.

Failures from pytest, ruff, and Vitest include the first failure's
source location, title, and bounded detail. Up to 20 further failures are shown
as one-line entries, with a count for anything hidden by the limit. The first
failure detail is limited to 20 lines, keeping the code frame and error message
when a traceback is longer. The Vitest reader supports `vitest [run|--run]`,
`npx vitest`, `npm exec vitest`, `pnpm exec vitest`, and `vp test`.

When the command is not recognised, or its output cannot be parsed, the final
15 log lines are shown instead. The complete combined output remains in the
private log file. Text and JSON failures include the same report, with JSON
placing it in `error.data.failure`; the run ID and log path remain in the error
message and data.

## Retrieve past runs

Read saved runs without running the command again. `runs` lists the 20 newest
runs for the current repository by default. Pass `--limit N` to choose another
positive number of runs.

```sh
agent-run runs
agent-run runs --limit 5
agent-run runs --limit 5 --json
```

Use the run ID from the list to inspect its record, complete log, or failure
details:

```sh
agent-run show RUN_ID
agent-run log RUN_ID
agent-run failures RUN_ID
```

`show` returns the stored command, status, duration, and log path. `log`
returns the complete saved log. `failures` runs the log through the matching
failure reader and returns only the failure detail. A passed run reports that
it has no failures. Add `--json` to any of these commands for the shared JSON
envelope.

A run that is still going shows status `running` without an exit status or duration.

Logs are stored in `agent-run-logs` beside the database. With the default
database, logs are written to `~/.agents/agent-run-logs`; setting
`AGENT_RUN_DATABASE` moves the log directory beside the selected database.

## Preview log retention

`prune` previews the saved runs that the fixed retention policy would remove.
Plain `prune` never deletes anything. Add `--apply` to remove the selected
runs and their logs.

```sh
agent-run prune
agent-run prune --json
agent-run prune --apply
agent-run prune --apply --json
```

The policy first selects saved runs that are still marked as running although
the agent-run process that owned them has gone, with the reason `abandoned`. A
live running run is never selected, including by the age and size rules. The
policy then selects saved runs older than 7 days. If saved-run logs add up to
more than 25 MB, it selects the oldest remaining saved runs until the saved-run
total would be at or under that limit. The preview covers every saved run in the
database, including runs from other repositories. It also selects `.log` files
with no saved run record when their file modification time is more than 7 days
old. Fresh logs without a saved run record remain untouched so an active run can
finish safely. The reported total log size includes every `.log` file in the
shared directory. The limits are fixed module constants, and there are no
settings for changing them.
Both output modes report whether deletion was applied, the total log size after
deletion, each selected run or orphan log's reason and space freed, and the
total space freed. If a run or log cannot be removed, `--apply` stops at that
item. Runs removed before the failure stay removed and are named in the error.
The error also names orphan logs removed before the failure. A run record that
has already gone is skipped, so applying the command again is safe.
