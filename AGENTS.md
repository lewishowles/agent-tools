# Agent tools

## Purpose

A collection of independently installed command-line tools that make common agent tasks faster, safer, or substantially smaller in context than using ordinary human tools directly.

## Boundaries

- Each package has one clear agent workflow.
- Tools run globally and do not require project-local shims.
- Default output is bounded; deeper evidence remains available on demand.
- Tools support stable JSON output.
- Broader developer tools and browser experiments belong in `dev-tools`.

## Gotchas

- Bare `pytest` and `ruff` are not on `PATH`. Run `uv run pytest packages/<package>` and `uv run ruff …` from the workspace root.
- In examples, docs, and tests, `--json` is the last option: `agent-run list --json`. For commands that take a `--` separator it goes immediately before `--`, because everything after `--` belongs to the stored command: `agent-run add build --cwd tools --json -- ruff check`.
- `progress` stores project releases, tasks, chunks, and handoff notes in `~/.agents/progress.db` by default. Set `AGENTS_PROGRESS_DATABASE` or pass `--database` to use another database. It binds each project to a working directory through local Git configuration, so run it inside a Git repository.
- `progress` (packages/progress) is installed as an editable uv tool from this repository. Source edits take effect in the installed `progress` command straight away; no reinstall is needed after changing it.
