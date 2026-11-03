# Changelog

All notable changes to `page-to-markdown` are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Remove blocks inside the chosen content whose class or ID labels them as page chrome, such as cookie notices, sign-ups, sharing controls, related posts, promotions, sponsorships, ads and pop-ups. Labels match whole words. Headings, blocks with a heading inside, large blocks and blocks labelled as content are kept.
- Count blocks removed by class or ID in the removed-elements report line.

### Fixed

- A `main`, `article` or `role="main"` element that holds less than a fifth of the page's text is now skipped, so a page whose main element is only a small banner keeps its article.
- Text inside elements marked `aria-hidden="true"`, such as icon labels and duplicate menus, is now left out of converted pages.

## [0.1.2] - 2026-08-25

### Fixed

- Kept long paths on one line in CLI output so they remain readable and copyable.

## [0.1.1] - 2026-08-04

### Fixed

- Escaped text when re-serialising selected content so literal markup is preserved correctly.

## [0.1.0] - 2026-07-22

### Added

- Added URL, file and stdin input modes for converting selected page content to compact Markdown.
- Added confidence reporting, fixture-based extraction tests and optional clipboard output.
- Added `cli-style` output formatting and support for combining multiple sources.
