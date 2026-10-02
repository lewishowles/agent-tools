"""
CLI entry point for page-to-markdown.

Handles input from a URL, local file, or stdin, selects the main content
region, converts it to Markdown, and writes it to stdout or a file.
"""

import argparse
import json
import sys
from dataclasses import dataclass

from page_to_markdown.clipboard import ClipboardError, copy_to_clipboard
from page_to_markdown.convert import convert_to_markdown
from page_to_markdown.fetch import FetchError, fetch_url, read_file
from page_to_markdown.report import ConfidenceReport, build_metadata, build_report
from page_to_markdown.select import select_content
from page_to_markdown.style import hint, row, row_group, span, status


@dataclass
class SourceResult:
	"""Store one source's report, content, or fetch failure."""

	# Source URL or file path used for extraction.
	source: str
	# Confidence report for a successful extraction.
	report: ConfidenceReport | None
	# Original HTML returned by the source.
	raw_content: str | None = None
	# HTML retained after content selection.
	selected_content: str | None = None
	# Markdown converted from the selected HTML.
	content: str | None = None
	# Fetch failure when the source could not be read.
	error: FetchError | None = None


def build_parser():
	"""Build the argument parser for the page-to-markdown command."""
	parser = argparse.ArgumentParser(
		prog="page-to-markdown",
		description="Convert a URL, file, or piped HTML into compact Markdown.",
	)
	parser.add_argument(
		"source",
		nargs="*",
		help="URL or paths to HTML files. Mutually exclusive with --stdin.",
	)
	parser.add_argument(
		"--stdin",
		action="store_true",
		help="Read HTML from stdin. Mutually exclusive with a source argument.",
	)
	parser.add_argument(
		"--output",
		default=None,
		help="Write output to this path instead of stdout.",
	)
	parser.add_argument(
		"--copy",
		action="store_true",
		help="Copy generated Markdown to the system clipboard.",
	)
	parser.add_argument(
		"--confidence",
		action="store_true",
		help="Also print the extraction confidence report to stdout.",
	)
	parser.add_argument(
		"--metadata",
		action="store_true",
		help="Write title, URL, and timestamp metadata beside --output.",
	)
	return parser


# Map confidence verdicts to cli-style result tones.
_VERDICT_TONES = {
	"high-confidence": "success",
	"medium-confidence": "warning",
	"low-confidence": "failed",
}


def _render_report(result) -> str:
	"""Render one source's report fields directly through cli-style, no string parsing."""
	if result.error is not None:
		return row_group(
			[
				{"label": "Source", "value": result.source, "wrap": False},
				{
					"label": "Error",
					"value": str(result.error),
					"result": "failed",
					"wrap": False,
				},
			]
		)

	report = result.report
	output = row_group(
		[
			{"label": "Source", "value": report.source, "wrap": False},
			{
				"label": "Selected content root",
				"value": report.selected_content_root,
				"wrap": False,
			},
			{
				"label": "Removed elements",
				"value": f"{report.removed_elements_summary} (total={report.removed_elements_total})",
			},
			{"label": "Links", "value": str(report.links)},
			{"label": "Code blocks", "value": str(report.code_blocks)},
			{
				"label": "Verdict",
				"value": report.verdict,
				"result": _VERDICT_TONES.get(report.verdict, "info"),
			},
		]
	)

	return "\n".join([output, *(hint(reason) for reason in report.reasons)])


def _copy_preview(content):
	"""Build a formatted terminal preview: length row, muted preview, truncation hint.

	Source is omitted here since the confidence report on stderr already reports it.
	"""
	character_count = len(content)
	line_count = len(content.splitlines())
	truncated = character_count > 300
	preview_text = content[:300]

	if truncated:
		preview_text += "..."

	preview_lines = [
		row("Length", f"{character_count} characters, {line_count} lines"),
		"",
		span(preview_text, tone="muted"),
	]

	if truncated:
		preview_lines.append("")
		preview_lines.append(hint(f"{character_count - 300} more characters"))

	return "\n".join(preview_lines) + "\n"


def _format_success_block(result):
	"""Wrap one successful document for combined output."""
	title = (
		build_metadata(
			result.raw_content or "",
			result.selected_content or "",
			result.source,
		)["title"]
		or result.source
	)
	content = (result.content or "").rstrip("\n")

	return f"## {title}\n\nSource: {result.source}\n\n{content}"


def _format_failed_block(result):
	"""Show one failed source in its original batch position."""
	return (
		f"## Failed: {result.source}\n\n"
		f"Source: {result.source}\n\n"
		f"**Failed to fetch:** {result.error}"
	)


def main(argv=None):
	"""Convert requested sources and write Markdown and optional reports.

	Args:
		argv: Optional command-line arguments, excluding the executable name. Defaults to the process arguments.

	Returns:
		0 when at least one source converts successfully, or 1 when conversion or an output operation fails.
	"""
	parser = build_parser()
	args = parser.parse_args(argv)

	if args.stdin and args.source:
		parser.error("--stdin cannot be combined with a source argument")

	if not args.stdin and not args.source:
		parser.error("provide a URL/file argument or use --stdin")

	if args.metadata and not args.output:
		parser.error("--metadata requires --output PATH")

	if args.metadata and len(args.source) > 1:
		parser.error("--metadata only supports one source")

	sources = args.source if args.source else ["stdin"]
	results = []

	for source in sources:
		try:
			if args.stdin:
				raw_content = sys.stdin.read()
			elif source.startswith(("http://", "https://")):
				raw_content = fetch_url(source)
			else:
				raw_content = read_file(source)
		except FetchError as error:
			results.append(SourceResult(source=source, report=None, error=error))
			continue

		selected_content = select_content(raw_content)
		base_url = source if source.startswith(("http://", "https://")) else None
		content = convert_to_markdown(selected_content, base_url=base_url)
		report = build_report(raw_content, selected_content, content, source)

		results.append(
			SourceResult(
				source=source,
				report=report,
				raw_content=raw_content,
				selected_content=selected_content,
				content=content,
			)
		)

	for result in results:
		sys.stderr.write(f"{_render_report(result)}\n\n")

	successful_results = [result for result in results if result.content is not None]

	if not successful_results:
		return 1

	if len(sources) == 1:
		content = successful_results[0].content or ""
	else:
		blocks = [
			_format_success_block(result)
			if result.content is not None
			else _format_failed_block(result)
			for result in results
		]
		content = "\n\n---\n\n".join(blocks) + "\n"

	if args.confidence and not args.output:
		formatted = "\n\n".join(_render_report(result) for result in results)
		sys.stdout.write(f"{formatted}\n\n")

	if args.output:
		try:
			with open(args.output, "w", encoding="utf-8") as f:
				f.write(content)
		except OSError as e:
			sys.stderr.write(
				status("failed", "Could not write output", f"{args.output}: {e}") + "\n"
			)
			return 1

		if args.metadata:
			metadata_path = f"{args.output}.json"
			try:
				with open(metadata_path, "w", encoding="utf-8") as f:
					json.dump(
						build_metadata(
							successful_results[0].raw_content or "",
							successful_results[0].selected_content or "",
							successful_results[0].source,
						),
						f,
						ensure_ascii=False,
						indent=2,
					)
					f.write("\n")
			except OSError as e:
				sys.stderr.write(
					status(
						"failed",
						"Could not write metadata",
						f"{metadata_path}: {e}",
					)
					+ "\n"
				)
				return 1
	elif not args.copy:
		sys.stdout.write(content)

	if args.copy:
		preview = _copy_preview(content) if not args.output else ""

		try:
			copy_to_clipboard(content)
		except ClipboardError as error:
			if not args.output:
				sys.stdout.write(content)

			sys.stderr.write(
				status("failed", "Clipboard copy failed", str(error)) + "\n"
			)
			return 1

		if not args.output:
			sys.stdout.write(preview)
			sys.stdout.write("\n")

		sys.stdout.write(
			status(
				"success",
				"Copied to clipboard",
				f"{len(content)} characters, {len(content.splitlines())} lines",
			)
			+ "\n"
		)

	return 0


if __name__ == "__main__":
	sys.exit(main())
