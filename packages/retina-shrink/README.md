# Retina shrink

`retina-shrink` makes copied images smaller before you paste them into a chat, and halves Retina image files before an agent opens them. It runs on macOS.

Mac screenshots are saved at twice the size they appear on screen. A halved copy shows the same detail you saw and costs fewer image tokens: about half on current Claude models, and about a quarter less on GPT-5.4.

In path mode, an image whose horizontal and vertical DPI are both 144 or more is saved at half the width and height as a 72 DPI PNG. The command prints the copy's path, or the original path when the image is not marked Retina or the copy would not be smaller. Copies are kept in `~/Library/Caches/retina-shrink/`, so the same image is only halved once.

## Build and install

From `packages/retina-shrink`:

```sh
swift build -c release
mkdir -p ~/.local/bin
cp .build/release/retina-shrink ~/.local/bin/retina-shrink
```

Inside an agent sandbox, add `--disable-sandbox` to every `swift` command. Swift's package tool tries to start its own sandbox, which macOS does not allow inside another one.

## Usage

```sh
retina-shrink /path/to/screenshot.png
retina-shrink /path/to/screenshot.png --json
retina-shrink clipboard
retina-shrink clipboard --json
```

To shrink a file named `clipboard` in the current folder, use `retina-shrink ./clipboard`.

Path mode prints one path. Its JSON includes the path, whether the image was halved, and input and output sizes in bytes and pixels. Errors print a short message to stderr, or an error object with `--json`. The exit code is 2 for wrong arguments, 1 for a missing or unreadable image, and 3 for an execution failure such as an unwritable cache or clipboard, as described in [the CLI contract](../../docs/cli-contract.md).

Run `retina-shrink clipboard` after copying an image and before pasting it. A Retina image (144 DPI or more in both directions) is reduced to a quarter of its stored width and height; another image is reduced to half. The clipboard is replaced with a PNG only when it holds one image item and the new PNG uses fewer bytes. PNG or TIFF data may include a file link or private `dyn.*` types, as with CleanShot and Preview. A file link on its own may include only the file's name or path as text and its icon, as with Salamander and Finder. In that case the linked image file is read without changing it. Ordinary text, rich content, links to non-image files, multiple items, and images without byte savings stay untouched. The command prints one line explaining what happened. Clipboard JSON includes `action`, a `reason` when unchanged, and the original and new sizes in pixels and bytes when an image could be read.
