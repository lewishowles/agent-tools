# review-packet

`review-packet` drafts a brief, file list and check report for an outside review of a `progress` chunk. The draft command writes a Markdown brief for you to complete; it does not create a zip.

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

By default, the change set starts at the last commit and includes staged, unstaged and untracked files. Use `--base REF` to start at another Git revision. Repeat `--include PATH` to add specific repository files. The command prints the selected files and the absolute path to the brief at `review-packet/<repo>-<chunk-id>/REVIEW.md` under the system temporary folder. Edit the marked sections before sharing it. An existing brief is kept unless you pass `--force`. The draft also writes `manifest.json` beside `REVIEW.md`, recording the base, the changes, a hash of each selected file and one fingerprint; it prints a `Manifest:` line, and `--force` replaces the manifest too. It writes `verification.md` beside them with the author's check results and prints a `Verification:` line.

The brief ends with a fixed request for the outside reviewer to list files they had to ask for or guess at, and files in the packet they didn't need. You don't need to fill in this section before building the packet.

Before writing the brief, manifest or verification report, the draft checks the selected files and generated text for `.env` files (except `.env.example` and `.env.sample`), private key blocks, recognisable AWS, GitHub, Slack, npm and `sk-` keys, and absolute home paths. It also refuses selected links that lead outside the repository. When the scan finds material it cannot share, the command says why, lists each `path:line: kind`, exits with code `1` and leaves no new draft files. In JSON mode, the locations and kinds are in `error.data.findings`; matched text is never included. This scan cannot recognise every secret or client detail, so check the material yourself before sharing it.

The draft selects changed files that still exist, plus `AGENTS.md` and the root `README.md` when present. For each changed file, it also selects the nearest package's manifest, README and test configuration when those files exist. It adds files imported directly by changed Python, JavaScript and TypeScript files, and same-name tests beside a changed file or in its project's test folders. Imported files are not followed further. Files named with `--include` or in the settings file are added as well. Deleted files appear in the brief's change list, but cannot appear in the contents list.

The brief's **Callers** section lists existing files in the same package that directly import a changed file, including a deleted file or the old name of a renamed file. It also lists callers already in the packet. Callers from another package in the repository are not listed. The section says when no callers were found. Listing a caller does not add it to the packet or manifest.

The brief lists local imports it could not find, changed files whose imports it could not read, and imported or matched test files that link outside the repository under **Left out**. Imports of a sibling package in the same repository are a known limit: they aren't followed. A Python import of a sibling package is listed under **Left out**. JavaScript and TypeScript imports by package name count as outside packages, so they aren't listed. Imports inside changed `.vue` files and dynamic `import()` calls aren't followed. Imported images, fonts and other files that aren't code, JSON, `.vue` or `.css` stay out of the packet without a note.

Use `--json` for a machine-readable result. The JSON result contains the brief, manifest and verification paths, the selected files and the settings file name.

## Optional settings

Place `.review-packet.toml` at the repository root to name extra files and checks for the draft:

```toml
checks = ["test"]
include = ["docs/architecture.md"]
```

`include` adds repository files to the draft file list. `checks` names registered `agent-run` commands that the draft runs in order. A failed or timed-out check does not stop the draft. `verification.md` lists each command, its state, run time and first failure output, and says the reviewer has not re-run it. Manual and unregistered checks are listed without running them. When no checks are configured, the report says so. If a check changes a selected file after its hash is recorded, the draft exits with code `1` and lists the changed paths without writing draft files. The command prints `.review-packet.toml` when it loads the file, or `(none)` when there is no settings file.

## Exit codes

- `0`: The brief was written.
- `1`: The requested progress record was not found, the scan found material that cannot be shared, or a check changed a selected file.
- `2`: Invalid arguments, settings, base revision or included path, or an existing brief without `--force`.
- `3`: A Git, progress environment, internal or file system error prevented the draft.
