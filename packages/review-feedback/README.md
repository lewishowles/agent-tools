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

## Review patches

`review-patches` splits uncommitted work into proposed commits as patch files that a reviewer can check. Write a plan at `.agent/review-patches/plan.json` that assigns each changed file or hunk to one proposal. Keep the plan in an ignored folder, because an untracked plan would itself count as a changed file:

```json
{
  "proposals": [
    {
      "id": "first-change",
      "title": "First change",
      "changes": [{ "path": "src/example.py", "hunks": [0] }]
    },
    {
      "id": "new-file",
      "title": "New file",
      "changes": [{ "path": "src/new.py" }]
    }
  ]
}
```

Hunk numbers start at 0 and follow `git diff -U10`, so changes within about 20 lines of each other count as one hunk. Omit `hunks` to include the whole file. The plan must assign every changed file and hunk exactly once.

Create patches, check whether they still match the plan and worktree, or refresh one proposal after an edit:

```sh
review-patches create --plan .agent/review-patches/plan.json
review-patches check .agent/review-patches
review-patches refresh first-change --plan .agent/review-patches/plan.json
```

Patches and metadata go in `.agent/review-patches` by default; ignore that folder in Git. Relative paths are resolved from the repository root, not the current directory, and `check` reads `plan.json` from the folder it checks unless you pass `--plan`. `check` exits with 0 when every patch is fresh and 1 when any patch is stale. Creation refuses staged changes unless you pass `--staged-policy include`.

## Clipboard limitation

Clipboard access uses macOS `pbpaste` and `pbcopy`. `review-feedback add` and the `--copy` flags need those commands on `PATH`; on another platform, or when either command is unavailable, copy the selection or save the packet from standard output manually. The matching and rendering commands do not require clipboard output when you use `preview` without `--copy`.
