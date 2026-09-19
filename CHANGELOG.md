# Changelog

All notable changes to `progress` are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `release remove --force` removes every task in a release, its owned rows,
  and release-owned notes in one transaction.
- `task remove --force` removes a task and its owned rows, and makes dependants
  ready when they have no remaining unfinished dependencies.

### Changed

- Breaking: abbreviated flags no longer work anywhere in the CLI.
- Release, task and chunk remove and complete commands now accept multiple IDs,
  apply them in order in one transaction, and return one result per ID.
- `task add` and `task edit` now accept repeatable `--contract-step` and `--file` flags, plus an optional `--split-rationale`.
- `task edit --clear-split-rationale` removes a task's split rationale, and `--clear-files` now clears the whole file list rather than a single value.

### Removed

- Breaking: releases no longer have a purpose, risks or out-of-scope list, and `release add` and `release edit` no longer accept `--purpose`, `--risks`, `--out-of-scope` or their `--clear-*` flags. Upgrading the database adds existing purpose text and out-of-scope items to the end of each release overview; release risks are deleted.
- The unused `model_tier` task column is removed from databases that have it.
- Breaking: `--contract` and `--files` have been removed. Use `--contract-step` and `--file` instead.

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
