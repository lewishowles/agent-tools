# Agent tools

## Purpose

A collection of independently installed tools that make common agent tasks faster, safer, or substantially smaller in context than using ordinary human tools directly. `browser-feedback` is a browser extension that turns notes on a web page into Markdown feedback for agents.

## Boundaries

- Each package has one clear agent workflow.
- Command-line tools run globally and do not require project-local shims.
- Default output is bounded; deeper evidence remains available on demand.
- Command-line tools support stable JSON output; the browser extension copies a Markdown packet for agents.
- Broader developer tools and browser experiments belong in `dev-tools`.

## Gotchas

- Bare `pytest` and `ruff` are not on `PATH`. Run `uv run pytest packages/<package>` and `uv run ruff …` from the workspace root.
- In examples, docs, and tests, `--json` is the last option: `agent-run list --json`. For commands that take a `--` separator it goes immediately before `--`, because everything after `--` belongs to the stored command: `agent-run add build --cwd tools --json -- ruff check`.
- `progress` stores project releases, tasks, chunks, and handoff notes in `~/.agents/progress.db` by default. Set `AGENTS_PROGRESS_DATABASE` or pass `--database` to use another database. It binds each project to a working directory through local Git configuration, so run it inside a Git repository.
- `progress` (packages/progress) is installed as an editable uv tool from this repository. Source edits take effect in the installed `progress` command straight away; no reinstall is needed after changing it.
- Never run a task worktree's copy of `progress` (for example `uv run progress` inside a worktree) against the shared `~/.agents/progress.db`. Opening the database upgrades its schema, and the installed `progress` then refuses to open it for every team until the new code reaches `main`. Test worktree changes with the test suite or a temporary `--database`.
- `packages/retina-shrink` is a Swift package excluded from the uv workspace. In the agent sandbox, pass `--disable-sandbox` to every SwiftPM build or test command.
- `packages/browser-feedback` is a plain JavaScript browser extension excluded from the uv workspace. It runs from source without a build step.
