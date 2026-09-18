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
agent-run --json
agent-run repository
agent-run repository --json
```

`agent-run repository` finds the Git repository containing the current working
directory and assigns it a stable ID. The ID is stored in the clone-local Git
configuration under `agent-run.repository-id`, so it is never committed and a
separately cloned repository receives a different ID. Linked worktrees share
the main checkout's local configuration and therefore share its ID.

## Register commands

Add a named command from inside a Git repository. The command stores its
argument array and working directory, which defaults to the repository root;
pass `--cwd` to choose another directory inside it and `--timeout` to set the
timeout used by named runs. If you leave out `--timeout`, named runs use the
120-second default. Set `--capability file-list` when the command accepts file
paths appended with `--file` or `--glob`; commands use the `none` capability by
default.

```sh
agent-run add test --timeout 30 -- pytest
agent-run add lint --cwd tools --json -- ruff check
agent-run add lint-changed --capability file-list -- ruff check
```

Change a command's working directory, arguments, or timeout. Anything you leave
out stays as it is:

```sh
agent-run edit lint --cwd scripts
agent-run edit lint -- ruff check --fix
agent-run edit lint --timeout 10
agent-run edit lint-changed --capability none
```

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

Working directories must stay inside the repository. A symlinked directory is
stored as its resolved target.

## Run commands

Run a saved command by name or a direct argument-array command in the
foreground. For direct runs, `--cwd` selects a directory inside the current
repository. Named runs use the saved command's working directory. The effective
timeout is chosen in this order: `--timeout`, the saved command's timeout, then
the 120-second default.

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

Logs are stored in `agent-run-logs` beside the database. With the default
database, logs are written to `~/.agents/agent-run-logs`; setting
`AGENT_RUN_DATABASE` moves the log directory beside the selected database.
