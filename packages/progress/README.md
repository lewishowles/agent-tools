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
progress [--json] [--database <path>] {next,summary,show,checkout,project,release,task,chunk,inbox,discovery,decision,context}
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

`progress next` shows the project's default task while it remains in progress.
When no default task is in progress, it uses task position order across ready,
waiting, blocked, and needs-decision tasks. It never selects another
in-progress task automatically. Blocked results include the stored blocking
reason and dependency IDs so you can choose whether to unblock, move, or revise
the task. Pass `--task` to read a named ready or in-progress task without
changing the default.

```bash
progress next
progress next --task <task_id>
```

### `progress summary`

Show current work for every project stored in the database. Projects with a
current task appear first, followed by projects with no current task. Each block
shows any recorded checkout paths beside the project name, the task and its
status, the active chunk and its position or completed chunk count, the release,
and any other unfinished task counts. A checkout whose path no longer exists is
marked `(stale)`. The command works outside a Git repository and does not record
a checkout. Paths under your home directory start with `~` in human output. Use
`--json` for per-project data. It includes full checkout paths, each checkout's
boolean `stale` field and last-seen time, the next action, and the suggested
command.

```bash
progress summary
```

### `progress show`

Show a project, release, task, chunk, discovery, decision, or inbox note from
its ID alone:

```text
progress show <id> [--json] [--database <path>]
```

The ID is required, and the command never falls back to the current task or
chunk. A malformed ID, or one with an unknown prefix, returns `invalid-id`; a
valid ID with no matching record returns `not-found`. Release, task, and chunk
output matches their `get` commands.

### `progress checkout detach`

Remove named checkout paths from the summary, or remove every recorded path
that no longer exists. Named paths accept `~` and relative paths; a path that
was never recorded returns an error without removing any checkouts. Live paths
can be detached and will be recorded again the next time a project command runs
there. The command works outside a Git repository and does not record a checkout.
`--json` returns the detached paths as a list.

```text
progress checkout detach [PATH...] [--stale] [--json] [--database <path>]
```

Provide at least one path or `--stale`. They can be used together.

## Projects

Project commands bind progress records to a Git repository.

### `progress project init`

Create a project for the current Git repository:

```text
progress project init --slug <slug> --name <name> [--json] [--database <path>]
```

- `--slug <slug>`: short project identifier
- `--name <name>`: display name

If the repository is already bound to a project, this reports the bound project instead of failing (`--json` adds `already_initialised: true`).

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

See [Listing](#listing) for pagination details.

### `progress release remove`

Remove a release:

```text
progress release remove <release_id>... [--force] [--json] [--database <path>]
```

Pass one or more release IDs. They are removed in the order given in one
transaction, so a failure names the failing ID and rolls back every removal.
Without `--force`, removal is a hard delete that raises
`StillReferencedError` when any task refers to a release or the release owns
notes. The error names the blocking task or note IDs and suggests `--force`.
With `--force`, every task in the release is removed with its chunks, notes,
dependency edges, contract and file rows, followed by the release's notes. The
human output lists the deleted records grouped by
type. Forced removal does not ask for confirmation.

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

### `progress release move`

Reorder a release within the current project:

```text
progress release move <release_id> --before <release_id> [--json] [--database <path>]
progress release move <release_id> --after <release_id> [--json] [--database <path>]
```

Exactly one of `--before` or `--after` is required, and the target must be
another release in the same project. The move changes release positions
atomically, placing the selected release before or after the target and
renumbering the rest so the order stays gap-free. `progress release list`
reflects the new order.

### `progress release complete`

Move a planned or active release to `done`:

```text
progress release complete <release_id>... [--json] [--database <path>]
```

Pass one or more release IDs. They are completed in the order given in one
transaction. Completing a release that is already `done` is rejected, names
the failing ID and current status, and rolls back earlier completions. A release
is started by starting a task within it.

## Tasks

A task belongs to the current project and can belong to a release. It can have
chunks, notes, and dependencies.

### `progress task add`

Create a task:

```text
progress task add --slug <slug> --title <title> --overview <overview> --contract-step <contract_step> [--contract-step <contract_step> ...] [--file <file> ...] [--split-rationale <split_rationale>] [--verification <verification>] [--release <release_id> | --release-id <release_id>] [--depends-on <task_id> | --dependency <task_id>] [--position <position>] [--json] [--database <path>]
```

- `--slug <slug>`: stable slug stored on the task
- `--title <title>`: display title
- `--overview <overview>`: non-empty task summary
- `--contract-step <contract_step>`: non-empty task contract step; repeat for each step
- `--file <file>`: optional file covered by the task; repeat for each file
- `--split-rationale <split_rationale>`: reason for splitting the task; doctor expects every task to have one
- `--verification <verification>`: optional verification instructions
- `--release <release_id>` or `--release-id <release_id>`: associate the task with a release
- `--depends-on <task_id>` or `--dependency <task_id>`: add a dependency on another task
- `--position <position>`: optional ordering position. At an occupied position,
  the new task goes before the task already there in its release or unassigned
  queue. A position past the end, or no position, places the new task last.
  Every add renumbers that queue from 1 with no gaps, so the returned position
  can be lower than requested and a position of 0 becomes 1

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
progress task remove <task_id>... [--force] [--json] [--database <path>]
```

Pass one or more task IDs. They are removed in the order given in one
transaction, so a failure names the failing ID and rolls back every removal.
Without `--force`, removal is a hard delete and raises `StillReferencedError`
when any of these still refer to the task:

- a chunk
- a dependency edge where the task is either the dependent or the dependency
- a discovery or decision note

The error names every blocking child ID and suggests `--force`. With
`--force`, the task is removed with its chunks, notes, dependency edges,
contract and file rows. Dependants blocked only by the removed task become
ready. The human output lists the deleted records grouped by type.
The `--force` option still refuses to remove a task note that is superseded by
a note outside the cascade, so the superseding note must be removed first.

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

The forced pass removes all completed tasks, including tasks without notes or
dependency edges. It also removes their notes, dependency edges, and chunks
before removing any releases left with no tasks.
Release-owned notes are removed with those releases. An empty release that was
not affected by this command is not removed.

If a note on a task that is not done supersedes a note being force
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
progress task edit <task_id> [--overview <overview>] [--contract-step <contract_step> ...] [--file <file> ...] [--split-rationale <split_rationale>] [--verification <verification>] [--clear-files] [--clear-split-rationale] [--clear-verification] [--json] [--database <path>]
```

`--overview`, each `--contract-step`, and `--split-rationale` value must contain
text. `--overview` and `--contract-step` are required when creating a task and
cannot be cleared. Pass replacement values when one needs changing. Use
`--clear-split-rationale` to remove the rationale. Doctor reports every task
without one.

### `progress task start`

Start a task:

```text
progress task start <task_id> [--secondary] [--json] [--database <path>]
```

Starting a task requires the task to be `ready`, moves it to `in-progress`, and
activates its first pending chunk, when it has one. Unfinished dependencies
raise `UnresolvedDependenciesError`. Several tasks can be `in-progress` in
one project. An ordinary start makes the task the project's default when no
default task is in progress. If another task holds the default, use
`--secondary` to start without changing it. A secondary start does not claim
the default even when it is empty. Other tasks and their active chunks stay
as they are.

### `progress complete`

Complete one task or chunk by ID:

```text
progress complete <task_or_chunk_id> [--json] [--database <path>]
```

Task and chunk completion follow the same rules as their commands below. Other
object IDs are rejected without changing state.

### `progress task complete`

Complete a task:

```text
progress task complete <task_id>... [--json] [--database <path>]
```

Pass one or more task IDs or project slugs. They are completed in the order
given in one transaction. Each task moves to `done` only when it is `ready` or
`in-progress` and has no `pending` or `active` chunks. If a task cannot
complete, `PendingChunksError` names the failing task and its blocking chunk
IDs, and all earlier completions are rolled back.

### `progress task unblock`

Make a `blocked` or `needs-decision` task ready to start. Dependencies are
checked again before the transition. If any remain unfinished, the command is
rejected with `UnresolvedDependenciesError`, which names their task IDs:

```text
progress task unblock <task_id> [--json] [--database <path>]
```

### `progress task get`

Show one task by ID, or omit the ID to use the task selected by `progress next`:

```text
progress task get [<task_id>] [--json] [--database <path>]
```

If no task is selected, the command returns an error with a recovery hint.

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
progress task list [--status <status>] [--all] [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--status <status>`: filter by task status
- `--limit <limit>`: maximum number of tasks to return
- `--offset <offset>`: number of tasks to skip before returning results
- `--all`: show done tasks in full and remove the page limit; cannot be combined with `--limit` or `--offset`

The human list collapses done tasks into a count for each release unless you
filter by status or pass `--all`. Its page limit counts unfinished tasks, and
the final line shows project-wide status counts and the next task. JSON keeps
its usual list order and fields; with `--all`, `limit` is `null` and `has_more`
is `false`.

See [Listing](#listing) for pagination details.

## Chunks

A chunk is a unit of work within a task.

### `progress chunk add`

Add a pending chunk to a task:

```text
progress chunk add --task <task_id> --title <title> --description <description> --review-question <review_question> [--position <position>] [--json] [--database <path>]
```

- `--task <task_id>`: task that owns the chunk
- `--title <title>`: display title
- `--description <description>`: non-empty chunk description
- `--review-question <review_question>`: the one question a reviewer answers
  about this chunk. A question that needs "and" to join two separate concerns
  usually means the chunk should be split. Doctor reports pending and active
  chunks that have no review question
- `--position <position>`: optional ordering position. At an occupied position,
  the new chunk goes before the chunk already there. A position past the end, or
  no position, places the new chunk last. Every add renumbers the task's chunks
  from 1 with no gaps, so the returned position can be lower than requested and
  a position of 0 becomes 1

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
progress chunk complete <chunk_id>... [--json] [--database <path>]
```

Pass one or more chunk IDs. They are completed in the order given in one
transaction. Completing an active chunk moves it to `done` and activates the
next pending chunk when one exists. A failure names the failing ID and rolls
back earlier completions.

### `progress chunk remove`

Remove a chunk:

```text
progress chunk remove <chunk_id>... [--json] [--database <path>]
```

Pass one or more chunk IDs. They are removed in the order given in one
transaction, so a failure names the failing ID and rolls back every removal.
Chunks have no referencing child rows, so a chunk can be removed directly.
Removal is a hard delete and never cascades.

### `progress chunk rename`

Change a chunk title:

```text
progress chunk rename <chunk_id> --title <title> [--json] [--database <path>]
```

- `--title <title>`: replacement display title

### `progress chunk edit`

Replace a chunk description, review question, or both:

```text
progress chunk edit <chunk_id> [--description <description>] [--review-question <review_question>] [--json] [--database <path>]
```

- `--description <description>`: non-empty replacement description
- `--review-question <review_question>`: non-empty replacement review question

Pass at least one of the two. Chunk descriptions and review questions cannot be
cleared; pass replacement text when one needs changing.

### `progress chunk list`

List chunks for a task, or omit `--task` to use the task selected by `progress next`:

```text
progress chunk list [--task <task_id>] [--all] [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

If no task is selected, the command returns an error with a recovery hint.

- `--task <task_id>`: task whose chunks should be listed
- `--limit <limit>`: maximum number of chunks to return
- `--offset <offset>`: number of chunks to skip before returning results
- `--all`: show done chunks in full and remove the page limit; cannot be combined with `--limit` or `--offset`

The human list shows chunks in reverse position order, numbered by their
stored position so the next chunk appears last. It collapses done chunks into
one count line, counts only unfinished chunks towards the page limit, and ends
with status counts and the next chunk. Pass `--all` to show done chunks in full.
JSON keeps its usual list order and fields; with `--all`, `limit` is `null` and
`has_more` is `false`. JSON output also includes a `task` object with the ID
and title of the listed task. See [Listing](#listing) for pagination details.

### `progress chunk get`

Show one chunk by ID, or omit the ID to use the chunk selected by `progress next`:

```text
progress chunk get [<chunk_id>] [--json] [--database <path>]
```

If no chunk is selected, the command returns an error with a recovery hint.

### `progress search`

Search tasks and chunks by a case-insensitive term:

```text
progress search <term> [--in <field>]... [--status <status>] [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--in <field>`: restrict the search to a field, and repeat it to search multiple fields
- `--status <status>`: filter by task status, including chunks whose parent task has that status
- `--limit <limit>`: maximum number of results to return
- `--offset <offset>`: number of results to skip before returning matches

The human output says "No matches." when nothing hits; otherwise, it shows one block per task or chunk with the type, status, title, ID, and parent task for chunks, followed by one line per matched field with a short snippet and the term in bold. The `--json` form returns the same rows with plain-text snippets.

## Inbox

Save thoughts for the current project without choosing a task or release.

### `progress inbox add`

Add an inbox note:

```text
progress inbox add <text>... [--json] [--database <path>]
```

- `<text>...`: note text made from the remaining arguments

The command returns the new note ID.

### `progress inbox list`

List inbox notes, oldest first:

```text
progress inbox list [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--limit <limit>`: maximum number of notes to return
- `--offset <offset>`: number of notes to skip before returning results

Bare `progress inbox` does the same and accepts the same options.
The human output shows each note's text, ID and creation time, and says
"No inbox notes." when the inbox is empty. The `--json` form returns the
paginated note rows. See [Listing](#listing) for pagination details.

### `progress inbox dismiss`

Dismiss an inbox note after reviewing it:

```text
progress inbox dismiss <note_id> [--json] [--database <path>]
```

- `<note_id>`: the inbox note ID

The command returns the dismissed note ID. An unknown or already dismissed ID
returns a `not-found` error.

## Notes

Each note belongs to exactly one task or release. A note is either a discovery
or a decision.

### `progress discovery add`

Add a discovery note:

```text
progress discovery add (--release <release_id> | --task <task_id>) [--json] [--database <path>] <body>...
```

- Exactly one of `--release <release_id>` or `--task <task_id>` is required.
- `--release <release_id>`: release that owns the note
- `--task <task_id>`: task that owns the note
- `<body>...`: note text made from the remaining arguments

### `progress discovery list`

List discovery notes:

```text
progress discovery list [--release <release_id> | --task <task_id>] [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--release <release_id>`: filter notes to a release
- `--task <task_id>`: filter notes to a task
- `--limit <limit>`: maximum number of notes to return
- `--offset <offset>`: number of notes to skip before returning results

Pass at most one of `--release` or `--task`. Without either filter, every
discovery note in the current project is listed. See [Listing](#listing) for
pagination details. The human output shows each note's body followed by its
owning task or release, and says "No discovery notes." when no notes match.
The `--json` form returns the paginated note rows.

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
progress decision add (--release <release_id> | --task <task_id>) [--supersedes <note_id>] [--json] [--database <path>] <body>...
```

- Exactly one of `--release <release_id>` or `--task <task_id>` is required.
- `--release <release_id>`: release that owns the note
- `--task <task_id>`: task that owns the note
- `--supersedes <note_id>`: note superseded by this decision
- `<body>...`: note text made from the remaining arguments

### `progress decision list`

List decision notes:

```text
progress decision list [--release <release_id> | --task <task_id>] [--limit <limit>] [--offset <offset>] [--json] [--database <path>]
```

- `--release <release_id>`: filter notes to a release
- `--task <task_id>`: filter notes to a task
- `--limit <limit>`: maximum number of notes to return
- `--offset <offset>`: number of notes to skip before returning results

Pass at most one of `--release` or `--task`. Without either filter, every
decision note in the current project is listed. See [Listing](#listing) for
pagination details. The human output shows each note's body followed by its
owning task or release, and says "No decision notes." when no notes match.
The `--json` form returns the paginated note rows.

### `progress decision remove`

Remove a decision note:

```text
progress decision remove <note_id> [--json] [--database <path>]
```

Removal is a hard delete. It is rejected when another note supersedes this
note, and the error names those blocking note IDs. The operation is atomic.

## Handoff context

### `progress context set`

Replace the handoff for a task or the current project:

```text
progress context set [--task <task_id>] [--current-goal <current_goal>] [--previous-step <previous_step>] [--next-step <next_step>] [--standing-context <standing_context>] [--verify-with <verify_with>] [--stop-marker <stop_marker>] [--json] [--database <path>]
```

- `--task <task_id>`: use the named task's handoff instead of the default task's
- `--current-goal <current_goal>`: current goal
- `--previous-step <previous_step>`: completed or last attempted step
- `--next-step <next_step>`: next step to take
- `--standing-context <standing_context>`: context that remains useful between steps
- `--verify-with <verify_with>`: command or evidence that verifies the work
- `--stop-marker <stop_marker>`: condition that tells the next agent when to stop

Without `--task`, `context get` and `context set` use the in-progress default
task's handoff. With no in-progress default task, they use the project's
handoff for planning or general handoff. `--task` selects any task in the
current project, including one that is not in progress. Each task has a
separate handoff. `context set` replaces only the selected handoff, clearing
any fields left out of the command. Results include `task_id` for a task
handoff and `null` for a project handoff.

Read the selected handoff with:

```text
progress context get [--task <task_id>] [--json] [--database <path>]
```

## Listing

The list commands support pagination with `--limit` and `--offset`. Task lists
also support `--status`, and `ready` lists the tasks that can start.
`--limit` defaults to `50` and accepts values from `1` to `200`.

## Statuses and note types

This table lists every legal literal for each status and note type, and the
command that sets or changes it.

| Field            | Literal          | Set or reached by                                                                                                 |
| ---------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------- |
| `release.status` | `planned`        | `release add --status planned`                                                                                    |
| `release.status` | `active`         | `release add --status active`                                                                                     |
| `release.status` | `done`           | `release add --status done`, or `release complete` from `planned` or `active`                                     |
| `task.status`    | `ready`          | `task add` when dependencies allow work, or `task unblock`                                                        |
| `task.status`    | `in-progress`    | `task start`                                                                                                      |
| `task.status`    | `waiting`        | `task add` or `task dependency add` when an unfinished dependency prevents work                                   |
| `task.status`    | `blocked`        | `task block` without `--needs-decision`; a manual block stays until `task unblock`, even when dependencies finish |
| `task.status`    | `needs-decision` | `task block --needs-decision`                                                                                     |
| `task.status`    | `done`           | `task complete`                                                                                                   |
| `chunk.status`   | `pending`        | `chunk add`, `chunk start` or `task block` returning an active chunk to pending                                   |
| `chunk.status`   | `active`         | `task start`, `chunk start`, or `chunk complete` activating the next pending chunk                                |
| `chunk.status`   | `done`           | `chunk complete`                                                                                                  |
| `chunk.status`   | `skipped`        | Schema-legal, but currently unreachable through any CLI command                                                   |
| `note.type`      | `discovery`      | `discovery add --release <release_id>` or `discovery add --task <task_id>`; immutable after creation              |
| `note.type`      | `decision`       | `decision add --release <release_id>` or `decision add --task <task_id>`; immutable after creation                |

The available transitions are:

- `release complete`: one or more `planned` or `active` releases → `done`; `done` → rejected, with the failing ID and current status named in the error
- `task start`: `ready` → `in-progress`; unfinished dependencies raise `UnresolvedDependenciesError`. Other in-progress tasks and their active chunks stay as they are
- `task block`: `ready` or `in-progress` → `blocked`, or → `needs-decision` with `--needs-decision`; an active chunk is returned to `pending`
- Unfinished dependencies make a task `waiting`. Once every dependency is `done`, the task becomes `ready` automatically; a manual `blocked` status remains until `task unblock`
- `task unblock`: `blocked` or `needs-decision` → `ready`; dependencies are re-checked, and unresolved dependencies reject the transition with `UnresolvedDependenciesError` naming the unfinished task IDs
- `task complete`: one or more `ready` or `in-progress` tasks with no `pending` or `active` chunks become `done`; pending or active chunks raise `PendingChunksError` naming the failing task and blocking chunk IDs
- `task start`: the first pending chunk becomes `active`
- `chunk start`: a pending chunk on an `in-progress` task becomes `active`, and another active chunk on that task returns to `pending`
- `chunk complete`: one or more pending or active chunks become `done`, and the next pending chunk becomes `active` when one exists

## Removal and errors

The release, task and chunk remove and complete commands accept one or more
space-separated IDs. Each command applies IDs in input order in one transaction,
and a failure names the failing ID before rolling back the whole command.

Without `--force`, all remove commands use hard deletion and preserve
referential integrity. A removal that would orphan a child is rejected before
deletion, and the whole operation is rolled back. `release remove --force` and
`task remove --force` delete the related rows described in their command
sections, still in one transaction.

| Command                                                 | Rejected when                                                           | Blocking IDs named in the error                  |
| ------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------ |
| `release remove <release_id>...`                        | Without `--force`, a task refers to the release or a note belongs to it | Referencing task or note IDs                     |
| `task remove <task_id>...`                              | Without `--force`, a chunk, dependency edge, or note refers to the task | Referencing chunk, task, dependency, or note IDs |
| `chunk remove <chunk_id>...`                            | Never; chunks have no referencing rows                                  | None                                             |
| `discovery remove <note_id>`                            | Another note supersedes the note                                        | Superseding note IDs                             |
| `decision remove <note_id>`                             | Another note supersedes the note                                        | Superseding note IDs                             |
| `task dependency remove <task_id> <depends_on_task_id>` | Never; removing an edge has no children                                 | None                                             |

With `--json`, these failures use the CLI's stable machine-readable error
envelope. Without it, the same reason and blocking IDs are shown in the
readable terminal response.
