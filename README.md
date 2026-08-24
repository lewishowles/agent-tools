# progress

`progress` is a local command-line tool for tracking project releases, tasks,
chunks, and handoff notes in SQLite. The default database is
`~/.agents/progress.db`.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Git, for project binding commands

## Getting started

From the `dev-tools` repository root, install the local package into your uv
tool environment:

```bash
uv tool install --reinstall --from packages/progress agents-progress
```

This puts `progress` on your `PATH` without publishing the package.

Bind the database to the Git repository where you are working:

```bash
progress project init --slug agents --name "Agent configuration"
```

Use `progress project attach <project_id>` when the project already exists in
the database and another Git checkout needs to use it.

## Command overview

The complete command shape is:

```text
progress [--json] [--database <path>] {next,project,release,task,chunk,discovery,decision,context}
```

Throughout this reference, commands use:

- `<value>` for a required value,
- `[--flag]` for an optional flag, and
- `...` for a body made from the remaining arguments.

Every subcommand accepts these common options:

- `--json`: return a machine-readable response instead of readable terminal output
- `--database <path>`: use `<path>` instead of `~/.agents/progress.db`
- `AGENTS_PROGRESS_DATABASE`: use this path when `--database` is not provided

Database path precedence is `--database`, then `AGENTS_PROGRESS_DATABASE`, then
the default `~/.agents/progress.db`.

Commands use a noun followed by a verb, such as `progress task list` or
`progress release get <release_id>`. If you enter a verb on its own, use a
legacy command name, or mistype a nested command, `progress` prints the valid
complete commands to try. The old `progress current` and `progress ready`
commands both point to `progress next`. With `--json`, the same suggestion is
returned in the standard error envelope.

## Current work

### `progress next`

Show the next unfinished task and its active chunk for the current project.

An in-progress task remains the first choice. Otherwise, `progress next` uses
task position order across ready, blocked, and needs-decision tasks. It does not
reorder tasks or change a blocked task's status. Blocked results include the
stored blocking reason and dependency IDs so you can choose whether to unblock,
move, or revise the task.

```bash
progress next
```

## Projects

Project commands bind progress records to a Git repository.

### `progress project init`

Create a project for the current Git repository:

```text
progress project init --slug <slug> --name <name> [--json] [--database <path>]
```

- `--slug <slug>`: short project identifier
- `--name <name>`: display name

### `progress project attach`

Attach the current Git repository to an existing project:

```text
progress project attach <project_id> [--json] [--database <path>]
```

- `<project_id>`: ID of the project to attach

### `progress project current`

Show the project attached to the current Git repository:

```text
progress project current [--json] [--database <path>]
```

## Releases

A release groups related tasks and has a title, slug, overview, position, and
status.

### `progress release add`

Create a release:

```text
progress release add --slug <slug> --title <title> --overview <overview> [--status {planned,active,done}] [--position <position>] [--json] [--database <path>]
```

- `--slug <slug>`: stable slug stored on the release
- `--title <title>`: display title
- `--overview <overview>`: non-empty release overview
- `--status {planned,active,done}`: initial release status
- `--position <position>`: optional ordering position

### `progress release list`

List releases for the current project:

```text
progress release list [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--limit <limit>`: maximum number of releases to return
- `--offset <offset>`: number of releases to skip before returning results

See [Listing](#listing) for output from this command.

### `progress release remove`

Remove a release:

```text
progress release remove <release_id> [--json] [--database <path>]
```

Removal is a hard delete. It raises `StillReferencedError` when any task still
refers to the release, and the error names the blocking task IDs. The operation
is atomic, so a rejected removal does not delete anything. Deletion never
cascades to tasks. Remove or move every referencing task before retrying.

### `progress release rename`

Change a release title without changing its slug or ID:

```text
progress release rename <release_id> --title <title> [--json] [--database <path>]
```

- `--title <title>`: replacement display title

### `progress release edit`

Replace a release overview:

```text
progress release edit <release_id> --overview <overview> [--json] [--database <path>]
```

- `--overview <overview>`: non-empty replacement overview

Release overviews are required and cannot be cleared. Pass replacement text
when the overview needs changing.

### `progress release complete`

Move a planned or active release to `done`:

```text
progress release complete <release_id> [--json] [--database <path>]
```

Completing a release that is already `done` is rejected and the error names its
current status. A release is started by starting a task within it.

## Tasks

A task belongs to the current project and can belong to a release. It can have
chunks, notes, and dependencies.

### `progress task add`

Create a task:

```text
progress task add --slug <slug> --title <title> --overview <overview> --purpose <purpose> --contract <contract> [--files <files>] [--acceptance-criteria <acceptance_criteria>] [--verification <verification>] [--risks <risks>] [--release <release_id> | --release-id <release_id>] [--depends-on <task_id> | --dependency <task_id>] [--position <position>] [--json] [--database <path>]
```

- `--slug <slug>`: stable slug stored on the task
- `--title <title>`: display title
- `--overview <overview>`: non-empty task summary
- `--purpose <purpose>`: non-empty task purpose
- `--contract <contract>`: non-empty task contract
- `--files <files>`: optional files covered by the task
- `--acceptance-criteria <acceptance_criteria>`: optional completion conditions
- `--verification <verification>`: optional verification instructions
- `--risks <risks>`: optional risks
- `--release <release_id>` or `--release-id <release_id>`: associate the task with a release
- `--depends-on <task_id>` or `--dependency <task_id>`: add a dependency on another task
- `--position <position>`: optional ordering position; when omitted, the task
  uses the first unused positive position in its release or unassigned queue

The new task is `ready` when its dependencies allow it to start, or `blocked`
when it still has unresolved dependencies.

Running `progress task add` at a real terminal without every required flag
prompts for whatever is missing, field by field, instead of raising the usual
missing-argument error. Already-supplied flags are skipped; optional fields
can be left blank by pressing Enter. Piped or non-interactive stdin (scripts,
CI, agents) always gets the missing-argument error instead of a prompt.

### `progress task move`

Move a task within its current release or unassigned queue:

```text
progress task move <task_id> --before <task_id> [--json] [--database <path>]
progress task move <task_id> --after <task_id> [--json] [--database <path>]
```

Exactly one of `--before` or `--after` is required when `--release` is not
used. The move changes task positions atomically, moving the selected task
before or after the target and shifting each task between its old and new
positions by one place.

Reassign a task to a release or the unassigned queue with the same command:

```text
progress task move <task_id> --release <release_id> [--before <task_id> | --after <task_id>] [--json] [--database <path>]
progress task move <task_id> --release [--before <task_id> | --after <task_id>] [--json] [--database <path>]
```

- `--release <release_id>`: target release; `--release` without a value, or
  `--release ""`, uses the unassigned queue
- `--before <task_id>` or `--after <task_id>`: optional position within the
  target release or unassigned queue; at most one may be provided when
  `--release` is used

When `--release` is used without a position target, the task is appended at
the first unused positive position in the target queue. A position target must
already belong to that release or the unassigned queue. The task's chunks,
notes, and dependency edges stay attached to it.

### `progress task dependency add`

Add a dependency:

```text
progress task dependency add <task_id> <depends_on_task_id> [--json] [--database <path>]
```

- `<task_id>`: task that depends on another task
- `<depends_on_task_id>`: task that must be completed first

### `progress task dependency remove`

Remove an existing dependency:

```text
progress task dependency remove <task_id> <depends_on_task_id> [--json] [--database <path>]
```

### `progress task remove`

Remove a task:

```text
progress task remove <task_id> [--json] [--database <path>]
```

Removal is a hard delete and raises `StillReferencedError` when any of these
still refer to the task:

- a chunk
- a dependency edge where the task is either the dependent or the dependency
- a discovery or decision note

The error names every blocking child ID. The operation is atomic, and deletion
never cascades to chunks, notes, or dependency edges. Remove every child row,
note, and dependency edge explicitly before retrying.

### `progress task clean`

Remove completed tasks that have no notes or dependency edges:

```text
progress task clean [--json] [--database <path>]
```

Tasks with discovery or decision notes, or with dependency edges, are kept
untouched. The human-readable result reports the removed and kept task counts,
each kept task's title and ID, the notes and dependency edges that kept it, and
any release that became empty because its tasks were removed. Dependency
details include the other task's title and ID and whether the task depends on
it or is required by it.

Use `--force` only when those notes and dependency edges can be deleted:

```text
progress task clean --force [--json] [--database <path>]
```

The forced pass removes the currently blocked completed tasks, their notes,
dependency edges, and chunks before removing any releases left with no tasks.
An empty release that was not affected by this command is not removed.

If a note on a task outside the blocked set supersedes a note being force
deleted, the whole `--force` pass aborts with a "still referenced" error and
nothing is deleted. This is rare and fails safely: resolve it by removing or
reassigning the superseding note first, then rerun `--force`.

### `progress task rename`

Change a task title:

```text
progress task rename <task_id> --title <title> [--json] [--database <path>]
```

- `--title <title>`: replacement display title

### `progress task edit`

Update task planning fields:

```text
progress task edit <task_id> [--overview <overview>] [--purpose <purpose>] [--contract <contract>] [--files <files>] [--acceptance-criteria <acceptance_criteria>] [--verification <verification>] [--risks <risks>] [--clear-files] [--clear-acceptance-criteria] [--clear-verification] [--clear-risks] [--json] [--database <path>]
```

`--overview`, `--purpose`, and `--contract` must contain text. These fields
are required and cannot be cleared. Pass replacement text when one needs
changing.

### `progress task start`

Start a task:

```text
progress task start <task_id> [--json] [--database <path>]
```

Starting a task requires the task to be `ready`, moves it to `in-progress`, and
activates its first pending chunk, when it has one. Unfinished dependencies
raise `UnresolvedDependenciesError`. The database permits only one
`in-progress` task per project: if another task already holds that status,
starting a new one demotes it back to `ready` (its active chunk, if any,
reverts to `pending`; completed chunks are untouched) in the same
transaction. The response's `demoted_task` field names the task that was
demoted, or is `null` when nothing was.

### `progress task complete`

Complete a task:

```text
progress task complete <task_id> [--json] [--database <path>]
```

This moves the task to `done` only when it is `in-progress` and has no `pending`
or `active` chunks. If chunks remain, `PendingChunksError` names the blocking
chunk IDs.

### `progress task unblock`

Make a `blocked` or `needs-decision` task ready to start. Dependencies are
checked again before the transition. If any remain unfinished, the command is
rejected with `UnresolvedDependenciesError`, which names their task IDs:

```text
progress task unblock <task_id> [--json] [--database <path>]
```

### `progress task get`

Show one task by ID:

```text
progress task get <task_id> [--json] [--database <path>]
```

### `progress task block`

Block a `ready` or `in-progress` task, optionally marking that it needs a
decision. If the task is `in-progress`, its active chunk returns to `pending`:

```text
progress task block <task_id> --reason <reason> [--needs-decision] [--json] [--database <path>]
```

- `--reason <reason>`: reason for blocking the task
- `--needs-decision`: use the `needs-decision` status instead of `blocked`

### `progress task list`

List tasks for the current project:

```text
progress task list [--status <status>] [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--status <status>`: filter by task status
- `--limit <limit>`: maximum number of tasks to return
- `--offset <offset>`: number of tasks to skip before returning results

See [Listing](#listing) for output from this command.

## Chunks

A chunk is a unit of work within a task.

### `progress chunk add`

Add a pending chunk to a task:

```text
progress chunk add --task <task_id> --title <title> --description <description> [--position <position>] [--json] [--database <path>]
```

- `--task <task_id>`: task that owns the chunk
- `--title <title>`: display title
- `--description <description>`: non-empty chunk description
- `--position <position>`: optional ordering position; when omitted, the chunk
  uses the first unused positive position in its task

Running `progress chunk add` at a real terminal without every required flag
prompts for whatever is missing, the same way `progress task add` does.

### `progress chunk move`

Move a chunk within its task:

```text
progress chunk move <chunk_id> --before <chunk_id> [--json] [--database <path>]
progress chunk move <chunk_id> --after <chunk_id> [--json] [--database <path>]
```

Exactly one of `--before` or `--after` is required. The move changes chunk
positions atomically, moving the selected chunk before or after the target and
shifting each chunk between its old and new positions by one place.

### `progress chunk start`

Activate a pending chunk:

```text
progress chunk start <chunk_id> [--json] [--database <path>]
```

The chunk's task must already be `in-progress`. If another chunk on the same
task is active, it returns that chunk to `pending` before activating the
requested chunk.

### `progress chunk complete`

Complete a chunk:

```text
progress chunk complete <chunk_id> [--json] [--database <path>]
```

Completing an active chunk moves it to `done` and activates the next pending
chunk when one exists.

### `progress chunk remove`

Remove a chunk:

```text
progress chunk remove <chunk_id> [--json] [--database <path>]
```

Chunks have no referencing child rows, so a chunk can be removed directly.
Removal is a hard delete and never cascades.

### `progress chunk rename`

Change a chunk title:

```text
progress chunk rename <chunk_id> --title <title> [--json] [--database <path>]
```

- `--title <title>`: replacement display title

### `progress chunk edit`

Replace a chunk description:

```text
progress chunk edit <chunk_id> --description <description> [--json] [--database <path>]
```

- `--description <description>`: non-empty replacement description

Chunk descriptions are required and cannot be cleared. Pass replacement text
when the description needs changing.

### `progress chunk list`

List chunks:

```text
progress chunk list --task <task_id> [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--task <task_id>`: task whose chunks should be listed
- `--limit <limit>`: maximum number of chunks to return
- `--offset <offset>`: number of chunks to skip before returning results

See [Listing](#listing) for output from this command.

## Notes

Notes are attached to tasks. A note is either a discovery or a decision.

### `progress discovery add`

Add a discovery note:

```text
progress discovery add --task <task_id> [--json] [--database <path>] <body>...
```

- `--task <task_id>`: task that owns the note
- `<body>...`: note text made from the remaining arguments

### `progress discovery remove`

Remove a discovery note:

```text
progress discovery remove <note_id> [--json] [--database <path>]
```

Removal is a hard delete. It is rejected when another note supersedes this
note, and the error names those blocking note IDs. The operation is atomic.

### `progress decision add`

Add a decision note:

```text
progress decision add --task <task_id> [--supersedes <note_id>] [--json] [--database <path>] <body>...
```

- `--task <task_id>`: task that owns the note
- `--supersedes <note_id>`: note superseded by this decision
- `<body>...`: note text made from the remaining arguments

### `progress decision remove`

Remove a decision note:

```text
progress decision remove <note_id> [--json] [--database <path>]
```

Removal is a hard delete. It is rejected when another note supersedes this
note, and the error names those blocking note IDs. The operation is atomic.

## Handoff context

### `progress context set`

Set the current project's singleton handoff context:

```text
progress context set [--current-goal <current_goal>] [--previous-step <previous_step>] [--next-step <next_step>] [--standing-context <standing_context>] [--verify-with <verify_with>] [--stop-marker <stop_marker>] [--json] [--database <path>]
```

- `--current-goal <current_goal>`: current goal
- `--previous-step <previous_step>`: completed or last attempted step
- `--next-step <next_step>`: next step to take
- `--standing-context <standing_context>`: context that remains useful between steps
- `--verify-with <verify_with>`: command or evidence that verifies the work
- `--stop-marker <stop_marker>`: condition that tells the next agent when to stop

Calling `context set` replaces the stored singleton context for the current
project.

## Listing

The list commands support pagination with `--limit` and `--offset`. Task lists
also support `--status`, and `ready` lists the tasks that can start.
`--limit` defaults to `50` and accepts values from `1` to `200`.

### Releases

```text
$ progress release list --limit 20 --offset 0
Releases:
- First release [planned] (rel_WMA5n_KV1i0Ddtp4Iq3hJQ)
```

### Tasks

```text
$ progress task list --status ready --limit 20 --offset 0
Tasks:
- First task [ready] (tsk_nIHyevhUxTyEDLbyfXPhRQ)
```

### Chunks

```text
$ progress chunk list --task tsk_nIHyevhUxTyEDLbyfXPhRQ --limit 20 --offset 0
Chunks

– First chunk · chk_KkjpMXgs5qkPeUsEuNDIDg
```

## Statuses and note types

This table lists every legal literal for each status and note type, and the
command that sets or changes it.

| Field            | Literal          | Set or reached by                                                                                                                                                 |
| ---------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `release.status` | `planned`        | `release add --status planned`                                                                                                                                    |
| `release.status` | `active`         | `release add --status active`                                                                                                                                     |
| `release.status` | `done`           | `release add --status done`, or `release complete` from `planned` or `active`                                                                                     |
| `task.status`    | `ready`          | `task add` when dependencies allow work, or `task unblock`                                                                                                        |
| `task.status`    | `in-progress`    | `task start`                                                                                                                                                      |
| `task.status`    | `blocked`        | `task add` when dependencies block work, `task dependency add` when an unfinished dependency is added to a ready task, or `task block` without `--needs-decision` |
| `task.status`    | `needs-decision` | `task block --needs-decision`                                                                                                                                     |
| `task.status`    | `done`           | `task complete`                                                                                                                                                   |
| `chunk.status`   | `pending`        | `chunk add`, `chunk start`/`task block`/`task start` demoting an active chunk to pending                                                                          |
| `chunk.status`   | `active`         | `task start`, `chunk start`, or `chunk complete` activating the next pending chunk                                                                                |
| `chunk.status`   | `done`           | `chunk complete`                                                                                                                                                  |
| `chunk.status`   | `skipped`        | Schema-legal, but currently unreachable through any CLI command                                                                                                   |
| `note.type`      | `discovery`      | `discovery add`; immutable after creation                                                                                                                         |
| `note.type`      | `decision`       | `decision add`; immutable after creation                                                                                                                          |

The available transitions are:

- `release complete`: `planned` or `active` → `done`; `done` → rejected, with the current status named in the error
- `task start`: `ready` → `in-progress`; unfinished dependencies raise `UnresolvedDependenciesError`. The database enforces one in-progress task per project, so another `in-progress` task in the same project is demoted to `ready` (its active chunk, if any, returned to `pending`) in the same transaction, and named in the response's `demoted_task` field
- `task block`: `ready` or `in-progress` → `blocked`, or → `needs-decision` with `--needs-decision`; an active chunk is returned to `pending`
- `task unblock`: `blocked` or `needs-decision` → `ready`; dependencies are re-checked, and unresolved dependencies reject the transition with `UnresolvedDependenciesError` naming the unfinished task IDs
- `task complete`: an `in-progress` task with no `pending` or `active` chunks becomes `done`; pending or active chunks raise `PendingChunksError` naming their blocking chunk IDs
- `task start`: the first pending chunk becomes `active`
- `chunk start`: a pending chunk on an `in-progress` task becomes `active`, and another active chunk on that task returns to `pending`
- `chunk complete`: the active chunk becomes `done`, and the next pending chunk becomes `active` when one exists

## Removal and errors

All remove commands use hard deletion and preserve referential integrity. No
remove command cascades to related rows. A removal that would orphan a child is
rejected before deletion, and the whole operation is rolled back.

| Command                                                 | Rejected when                                        | Blocking IDs named in the error                  |
| ------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------ |
| `release remove <release_id>`                           | A task refers to the release                         | Referencing task IDs                             |
| `task remove <task_id>`                                 | A chunk, dependency edge, or note refers to the task | Referencing chunk, task, dependency, or note IDs |
| `chunk remove <chunk_id>`                               | Never; chunks have no referencing rows               | None                                             |
| `discovery remove <note_id>`                            | Another note supersedes the note                     | Superseding note IDs                             |
| `decision remove <note_id>`                             | Another note supersedes the note                     | Superseding note IDs                             |
| `task dependency remove <task_id> <depends_on_task_id>` | Never; removing an edge has no children              | None                                             |

With `--json`, these failures use the CLI's stable machine-readable error
envelope. Without it, the same reason and blocking IDs are shown in the
readable terminal response.
