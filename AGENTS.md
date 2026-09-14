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
