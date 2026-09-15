# agent-run

`agent-run` will discover, register, and run project commands while returning
bounded results and complete evidence for failures.

The command shell exposes help, repository identification, and the shared text
and JSON output contract. Command registration, targeting, execution, and
evidence arrive in later chunks.

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
