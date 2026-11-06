# review-packet

`review-packet` drafts a brief and file list for an outside review of a `progress` chunk. The draft command writes a Markdown brief for you to complete; it does not create a zip.

## Requirements

- Python 3.11 or newer
- `uv`
- A Git worktree with a `progress` chunk record

## Install

From the repository root:

```sh
uv tool install ./packages/review-packet
```

## Draft a brief

Run the command in the worktree you want reviewed:

```sh
review-packet draft <chunk-id> [--base REF] [--include PATH] [--force] [--json]
```

By default, the change set starts at the last commit and includes staged, unstaged and untracked files. Use `--base REF` to start at another Git revision. Repeat `--include PATH` to add specific repository files. The command prints the selected files and the absolute path to the brief at `review-packet/<repo>-<chunk-id>/REVIEW.md` under the system temporary folder. Edit the marked sections before sharing it. An existing brief is kept unless you pass `--force`. The draft also writes `manifest.json` beside `REVIEW.md`, recording the base, the changes, a hash of each selected file and one fingerprint; it prints a `Manifest:` line, and `--force` replaces the manifest too.

The draft selects changed files that still exist, plus `AGENTS.md` and the root `README.md` when present. For each changed file, it also selects the nearest package's manifest, README and test configuration when those files exist. It adds files imported directly by changed Python, JavaScript and TypeScript files, and same-name tests beside a changed file or in its project's test folders. Imported files are not followed further. Files named with `--include` or in the settings file are added as well. Deleted files appear in the brief's change list, but cannot appear in the contents list.

The brief lists local imports it could not find, changed files whose imports it could not read, and imported or matched test files that link outside the repository under **Left out**. Imports of a sibling package in the same repository are a known limit: they aren't followed. A Python import of a sibling package is listed under **Left out**. JavaScript and TypeScript imports by package name count as outside packages, so they aren't listed. Imports inside changed `.vue` files and dynamic `import()` calls aren't followed. Imported images, fonts and other files that aren't code, JSON, `.vue` or `.css` stay out of the packet without a note.

Use `--json` for a machine-readable result. The JSON result contains the brief path, selected files and settings file name.

## Optional settings

Place `.review-packet.toml` at the repository root to name extra files and checks for a later build step:

```toml
checks = ["test"]
include = ["docs/architecture.md"]
```

`include` adds repository files to the draft file list. `checks` names registered `agent-run` commands for a later build step to run; the draft ignores it. The command prints `.review-packet.toml` when it loads the file, or `(none)` when there is no settings file.

## Exit codes

- `0`: The brief was written.
- `1`: The requested progress record was not found.
- `2`: Invalid arguments, settings, base revision or included path, or an existing brief without `--force`.
- `3`: A Git, progress environment, internal or file system error prevented the draft.
