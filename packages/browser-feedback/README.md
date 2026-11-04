# Browser feedback

A Firefox extension for leaving element-specific review notes on a web page and copying them as one Markdown packet. It runs from source without a build step.

## Load in Firefox

1. Open `about:debugging` and choose **This Firefox**.
2. Choose **Load Temporary Add-on** and select `packages/browser-feedback/manifest.json`.
3. Open a web page and use the toolbar button or **Cmd/Ctrl+Shift+E** to start picking. You may need to pin the button to the toolbar.

Firefox removes temporary add-ons when the browser closes. Reload the add-on from `about:debugging` when needed.

## Leave feedback

Hover to highlight an element, then click to select it, which also stops picking; choose **Pick elements** in the panel to pick again. Shift-click instead to keep picking and add another element to the same note, or to remove one you already selected. Write an instruction in the panel and choose **Save note**. Use **Escape** to cancel the selection or an edit and stop picking. The panel also lets you edit or delete saved notes.

Notes are stored in extension storage by page origin and pathname, so they survive page reloads. Query strings and fragments do not split notes into separate pages. **Copy all** copies a numbered Markdown packet containing the page URL, viewport, selectors, element details and comments, then clears the saved notes. The packet may contain text and HTML from the page; review it before sharing.

The extension runs on ordinary web pages. Browser-owned pages and some restricted pages do not allow content scripts, so the picker is unavailable there.

## What's in a copied packet

Each numbered entry has a heading with the page URL, viewport size in CSS pixels and the selector of each chosen element. A JSON block contains one snapshot for each selected element:

- `selector`: a selector that matches only this element, starting from the nearest unique ID (or from `html` when there is none)
- `tag`, `id`, `classes` and `role`: the element's tag name and identifiers
- `text`: the text shown on screen, including the labels of buttons inside the element, limited to 240 characters. An element with no text falls back to its ARIA label, except a form field, which reports empty text
- `attributes`: useful attributes such as `alt`, `aria-label`, `href` and `name`, with values limited to 240 characters
- `outerHTML`: the element's HTML, marked `(truncated)` if it exceeds 1,600 characters
- `parent`: the parent's tag, ID and classes
- `previousSibling` and `nextSibling`: the neighbouring tag names
- `styles`: computed colour, font, display and position styles
- `bounds`: position and size in CSS pixels

The note's comment follows the JSON block.
