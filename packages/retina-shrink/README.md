# Retina shrink

`retina-shrink` halves Retina screenshots so agents read a smaller image. It runs on macOS.

Mac screenshots are saved at twice the size they appear on screen. A halved copy shows the same detail you saw and costs fewer image tokens: about half on current Claude models, and about a quarter less on GPT-5.4.

When an image's horizontal and vertical DPI are both 144 or more, `retina-shrink` saves a copy at half the width and height as a 72 DPI PNG and prints the copy's path. It prints the original path instead for any other image, or when the halved copy would not be smaller. Copies are kept in `~/Library/Caches/retina-shrink/`, so the same image is only halved once.

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
```

The default output is one path. JSON includes the path, whether the image was halved, and input and output sizes in bytes and pixels. Errors print a short message to stderr, or an error object with `--json`. The exit code is 2 for wrong arguments, 1 for a missing or unreadable image, and 3 when the halved copy cannot be saved, as described in [the CLI contract](../../docs/cli-contract.md).
