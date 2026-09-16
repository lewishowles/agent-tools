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
pass `--cwd` to choose another directory inside it.

```sh
agent-run add test -- pytest
agent-run add lint --cwd tools --json -- ruff check
```

Change a command's working directory, its arguments, or both. Anything you
leave out stays as it is:

```sh
agent-run edit lint --cwd scripts
agent-run edit lint -- ruff check --fix
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

Run a direct argument-array command in the foreground. A positive `--timeout`
in seconds is required, and `--cwd` selects a directory inside the current
repository.

```sh
agent-run run --timeout 30 -- pytest
agent-run run --cwd tools --timeout 10 --json -- ruff check
```

Text output includes the command's combined standard output and standard error,
followed by its exit status. JSON output returns the captured output and run
details in `data`. Until complete private log files and run IDs arrive, output
capture is provisional and a JSON failure returns only the error message.
