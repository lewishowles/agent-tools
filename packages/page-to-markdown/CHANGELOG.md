# Changelog

All notable changes to `page-to-markdown` are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
