# review-feedback

`review-feedback` records comments against exact locations in a Git worktree and renders one Markdown packet for an agent or review workflow. It stores the copied source text in the draft so entries can follow incidental line shifts before output.

## Requirements

- macOS for clipboard input and `--copy`
- Python 3.11 or newer
- `uv`
- A Git worktree

## Install

From the repository root, install the local package as a tool:

```sh
uv tool install ./packages/review-feedback
```

## Usage

1. In your Git tool of choice, select the source text you want to review and copy it.
2. In a terminal opened in the same repository, run `review-feedback add` and enter the comment when prompted.
3. Repeat the first two steps for each comment.
4. Review the packet without changing the draft:

```sh
review-feedback preview
```

5. When the packet is ready, copy it and retire the draft:

```sh
review-feedback finish --copy
```

Use `review-feedback preview --copy` if you want to copy the packet while keeping the draft active. A successful `finish` moves the draft to trash, so the next `add` starts a new review.

## Recover from a failed selection

The copied text can match one or more locations; `add` creates one entry per match, all sharing the same comment. When the command reports a failure, no entry is added:

- **Not found:** copy the exact text again from the current Git view, including enough surrounding context, then run `review-feedback add`.
- **Spans both sides:** the selection combines current and removed content. Copy one side at a time and add separate comments.
- **Stale during show, preview, or finish:** the file changed after capture. A unique match is relocated automatically. If the text appears at several locations, the command lists every candidate and keeps the cached location; remove and re-add the entry once you've made the text unique, or wait for a further edit to disambiguate it. If the text is missing, `show` reports it and preview or finish reports every missing entry together without writing a packet.

The packet uses the working-tree path and coordinates for current content. Removed content is marked `(removed at HEAD)` and uses its `HEAD` path and coordinates.

## Clipboard limitation

Clipboard access uses macOS `pbpaste` and `pbcopy`. `review-feedback add` and the `--copy` flags need those commands on `PATH`; on another platform, or when either command is unavailable, copy the selection or save the packet from standard output manually. The matching and rendering commands do not require clipboard output when you use `preview` without `--copy`.
