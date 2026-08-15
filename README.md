# progress

`progress` is a local command-line tool for tracking project releases, tasks,
chunks, and handoff notes in SQLite. The default database is
`~/.agents/progress.db`.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Git, for project binding commands

## Getting started

From the `dev-tools` repository root, install the local package into your uv tool
environment:

```bash
uv tool install --reinstall --from packages/progress agents-progress
```

This puts `progress` on your `PATH` without publishing the package.

## Usage

Initialise a project from its Git repository:

```bash
progress project init --slug agents --name "Agent configuration"
```

Create and work through a release, task, and chunks:

```bash
# Create a release. Use the returned ID in the task command below.
progress release add --slug progress-store --title "Progress store"

# Associate the task with that release.
progress task add --slug package-progress --title "Package the progress CLI" --release <release-id>

# Add the pending chunks that make up the task.
progress chunk add --task <task-id> --title "Add package metadata"
progress chunk add --task <task-id> --title "Document package usage"

# Starting the task automatically activates its first pending chunk.
progress task start <task-id>

# Inspect the active task and chunk.
progress next

# Completing the active chunk automatically activates the next pending chunk.
progress chunk complete <chunk-id>

# Complete the newly active chunk, then record the discovery and finish the task.
progress chunk complete <next-chunk-id>
progress discovery add --task <task-id> "The local package is installed from the workspace."
progress task complete <task-id>
```

Common options:

- `--json`: return a stable machine-readable response instead of readable terminal output
- `--database PATH`: use another SQLite database
- `--limit`: limit the number of results returned by list commands and `ready`
- `--offset`: skip results when paginating list commands and `ready`
- `task list --status STATUS`: filter tasks by status
