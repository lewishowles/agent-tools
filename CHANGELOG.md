# Changelog

All notable changes to `progress` are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-08-30

### Added

- `progress --version` global flag, in human and `--json` output.
- `remove` commands for releases, tasks, and chunks, which refuse to remove a release or task that is still referenced.
- `rename` commands for releases, tasks, and chunks.
- `release complete`, `release edit`, and `release move`.
- `task edit`, `task move` (including reassigning a task's release), and `task clean` to bulk-remove done tasks.
- `chunk edit`, `chunk move`, and `chunk start` to recover orphaned pending chunks.
- `doctor` command reporting blank required fields.
- `commands` manifest listing every command and flag for one-call agent discovery.
- Commands to view context, notes, releases, and chunks.
- `AGENTS_PROGRESS_DATABASE` environment variable for sandboxed environments.
- Task and release slugs accepted alongside IDs.
- Interactive, editable prompts for missing add and edit flags.

### Changed

- `progress next` now surfaces ready and unblockable work instead of nothing, ranks items by their queue order, prefers tasks in the active release, and reports blocked queue items.
- Completing a task's last dependency now unblocks the tasks that were waiting on it.
- Planning notes are now required when creating or editing releases, tasks, and chunks.
- Human-readable output routed through cli-style throughout: grouped styled tables, chunk descriptions in listings, aligned output, a blank line above every command's output, styled errors and completion lines, and graceful Ctrl+C handling.
- Bare invocation prints help instead of erroring.
- Unknown or legacy input suggests the right command.
- New tasks and chunks default to the first free position.
- `release list` hides completed releases by default.

### Removed

- `progress current` command (redundant with `progress next`).
- `progress ready` command.
- `model_tier` field.
