# page-to-markdown

Turn a URL, local HTML file, or piped HTML into compact Markdown. It picks out the page's main content, leaves out navigation and other page furniture, and reports a confidence verdict so you know what it picked and why.

It won't render JavaScript; for JS-rendered pages, pipe rendered HTML in from `web-audit render` instead (see [Combining with web-audit](#combining-with-web-audit)).

## Requirements

- Python 3.11+
- No other dependencies
- `--copy` needs macOS (uses `pbcopy`)

Styled confidence-report output requires the standalone [`cli-style` binary](https://github.com/lewishowles/cli-style/releases) to be available on `PATH`. When it is not installed, `page-to-markdown` falls back to plain text output, so no additional dependency is required.

## Getting started

Install it globally with `uv`:

```bash
uv tool install page-to-markdown
```

To install it into an existing Python environment with `pip`:

```bash
pip install page-to-markdown
```

## Basic usage

```bash
# A URL
page-to-markdown https://example.com/article

# A local HTML file
page-to-markdown ./page.html

# Piped HTML from another tool
cat page.html | page-to-markdown --stdin
```

By default the Markdown goes to stdout and a confidence report goes to stderr:

```
source: https://example.com/article
selected-content-root: article
removed-elements: nav=1, footer=1 (total=2)
links: 12
code-blocks: 0
verdict: high-confidence
```

`verdict` is one of `high-confidence`, `medium-confidence`, or `low-confidence`. A low-confidence verdict includes `reason:` lines explaining why (e.g. a JS app shell with almost no body text. Use `web-audit render` first in that case).

## What gets removed

- Scripts, styles, `noscript` blocks, and inline SVG.
- The page's header, footer, navigation, sidebars (`aside`), and forms.
- Anything marked `aria-hidden="true"`, such as icon labels and duplicate menus, because screen readers skip it too. Content hidden only by the `hidden` attribute or an inline `display` style is kept, because tabs and accordions use those for real content.

To choose the main content, it uses the first `main`, `article`, or `role="main"` element that holds at least a fifth of the page's text, checked in that order. If none does, it uses the whole body. This stops a small banner marked as `main` from hiding the real article.

It does not look for ads, cookie banners, share buttons, or related-post lists, so these can still appear in the output.

A page saved while a dialog was open can come out nearly empty. Sites often mark everything behind an open dialog `aria-hidden="true"`, so that content is removed along with it.

## Multiple sources at once

Pass more than one URL or file and they combine into a single Markdown document:

```bash
page-to-markdown https://example.com/one https://example.com/two ./local.html
```

Each source becomes its own block, separated by a rule:

```
## Article one title

Source: https://example.com/one

...converted content...

---

## Article two title

Source: https://example.com/two

...converted content...
```

The heading uses the page's `<title>` (or first heading) when available, falling back to the source itself: useful for newsletters/HTML emails that don't have a clean title.

If one source fails to fetch, the rest still convert; the failed one shows a `## Failed: <source>` block in its place instead of stopping the whole batch. The command exits `0` if at least one source succeeded, `1` only if every source failed. A single source's output is unchanged by this feature: no heading or wrapper is added.

## Flags

| Flag                 | Effect                                                                                                                                                                                                                                                   |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--stdin`            | Read HTML from stdin instead of a URL/file argument. Cannot be combined with source arguments.                                                                                                                                                           |
| `--output PATH`      | Write Markdown to a file instead of stdout.                                                                                                                                                                                                              |
| `--copy`             | Copy the generated Markdown to the clipboard (macOS only, via `pbcopy`). Without `--output`, prints a short formatted preview (length, a 300-character truncated excerpt, a success/failure status) instead of dumping the full content to the terminal. |
| `--confidence`       | Also print the confidence report to stdout (it always goes to stderr regardless of this flag).                                                                                                                                                           |
| `--metadata`         | Alongside `--output`, also write a `<output>.json` sidecar with `title`, `url`, and `timestamp`. Requires `--output`, and only supports a single source (ambiguous with a batch).                                                                        |
| `--list-headings`    | List each heading's level, plain text, and unique anchor selector from one source. Headings inside code blocks are skipped.                                                                                                                              |
| `--heading HEADING`  | Return one section by its anchor selector or heading text from one source. Text matching ignores case, repeated spaces, and inline Markdown. Duplicate text needs a selector.                                                                            |
| `--without-children` | With `--heading`, stop at the next heading of any level. By default, child sections are included.                                                                                                                                                        |
| `--json`             | Write the heading list or selected section as JSON. Requires `--list-headings` or `--heading`; cannot be combined with `--confidence` or `--copy`.                                                                                                       |

Use the selector from `--list-headings` to pick a duplicate heading:

```bash
page-to-markdown ./page.html --list-headings
page-to-markdown ./page.html --heading installation-1 --json
```

The section starts with the selected heading and ends before the next heading at the same or a higher level. `--json` returns `source`, `heading` (level, text, selector), and `markdown`. A missing or ambiguous heading exits `1` and reports up to five candidates. With `--json`, that failure is an `error` object containing `kind`, `query`, and `candidates`.

## Combining with web-audit

`page-to-markdown` does not render JavaScript. For JS-rendered pages, use `web-audit render` (from the `web-audit` package) to load the page in a real browser and print the rendered HTML, then pipe that in:

```bash
web-audit render https://example.com/js-heavy-page | page-to-markdown --stdin
```
