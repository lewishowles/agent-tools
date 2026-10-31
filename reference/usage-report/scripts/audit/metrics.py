#!/usr/bin/env python3
"""Reproduce the aggregate counts quoted in the session audit report.

Consolidates the one-off queries run during the audit so every number in the
report can be re-derived. Reads transcripts under ~/.claude/projects and
friction events through the friction CLI; writes nothing.

Usage:
    python3 scripts/audit/metrics.py [--days 21]

Each section prints the figure and the report finding it supports.
"""

import argparse
import collections
import datetime
import glob
import json
import os
import re
import statistics
import subprocess

# The Claude project directory containing session transcripts.
ROOT = os.path.expanduser("~/.claude/projects")
# The maximum command length used to compare repeated calls, shared with redundancy.py.
COMMAND_TEXT_LIMIT = 300
# The maximum tool result length included in flattened record text.
RESULT_TEXT_LIMIT = 3000


def session_paths(days):
	"""Transcript paths modified within the window, newest last.

	@param  {int}  days
	    Size of the window in days.
	"""
	# The earliest transcript modification time included in the window.
	cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
		days=days
	)

	# The transcript paths that fall within the requested window.
	paths = []

	for path in glob.glob(os.path.join(ROOT, "*", "*.jsonl")):
		# The transcript modification time checked against the cutoff.
		mtime = datetime.datetime.fromtimestamp(
			os.stat(path).st_mtime, datetime.timezone.utc
		)

		if mtime >= cutoff:
			paths.append(path)

	return sorted(paths, key=os.path.getmtime)


def records(path):
	"""Yield parsed JSONL records, skipping malformed lines."""
	with open(path, errors="replace") as handle:
		for line in handle:
			try:
				yield json.loads(line)
			except ValueError:
				continue


def blocks(record):
	"""Yield the content blocks of a user or assistant record."""
	# The message content, which may be text or structured blocks.
	content = (record.get("message") or {}).get("content")

	if isinstance(content, str):
		yield {"type": "text", "text": content}
	elif isinstance(content, list):
		for block in content:
			if isinstance(block, dict):
				yield block


def record_text(record):
	"""Flatten a record's text and tool-result content into one string."""
	# The text fragments joined into one searchable record string.
	parts = []

	for block in blocks(record):
		if block.get("type") == "text":
			parts.append(block.get("text", ""))
		elif block.get("type") == "tool_result" and isinstance(
			block.get("content"), str
		):
			parts.append(block["content"][:RESULT_TEXT_LIMIT])

	return " ".join(parts)


def tool_calls(record):
	"""Yield (name, input) for each tool call in an assistant record."""
	for block in blocks(record):
		if block.get("type") == "tool_use":
			yield block.get("name"), (block.get("input") or {})


def classify_bash_command(command):
	"""Return one stable, normalised identifier for a Bash command."""
	if not isinstance(command, str) or not command.strip():
		return None

	command = command.strip()

	# The npm test or lint command, when the command contains one.
	npm_match = re.search(r"\bnpm\s+(?:run\s+)?(test|lint)\b", command)
	if npm_match:
		return f"npm {npm_match.group(1)}"

	if re.search(r"\b(?:npx\s+)?vitest(?:\s+run)?\b", command):
		return "vitest"

	for segment in re.split(r"\s*(?:&&|\|\||[;|])\s*", command):
		# The words used to identify this command segment's executable.
		tokens = segment.strip().split()
		if not tokens:
			continue

		while tokens and tokens[0] in ("cd", "clear", "command", "env", "exec", "sudo"):
			tokens.pop(0)
			if (
				tokens
				and tokens[0].startswith("/")
				and tokens[0].endswith(("/bash", "/zsh", "/sh"))
			):
				tokens.pop(0)

		if tokens:
			return re.sub(r"[^A-Za-z0-9_.-]", "", tokens[0]) or None

	return None


def section(title, finding):
	print(f"\n{'=' * 72}\n{title}  [{finding}]\n{'=' * 72}")


# --- Section 1: corpus size -------------------------------------------------


def corpus(paths):
	section("Corpus", "Scope and method")

	# The project directories represented by the selected transcripts.
	projects = {os.path.basename(os.path.dirname(p)) for p in paths}

	print(f"sessions in window: {len(paths)}")
	print(f"project directories: {len(projects)}")


# --- Section 2: PROGRESS.md churn -------------------------------------------


def progress_churn(paths):
	section("PROGRESS.md churn", "F1")

	# The total PROGRESS.md edits across sessions that edited it more than once.
	total = 0
	# The number of sessions with repeated PROGRESS.md edits.
	sessions = 0
	# The edit and session counts grouped by day.
	by_day = collections.defaultdict(lambda: [0, 0])
	# Each session with repeated PROGRESS.md edits, sorted later to list the worst.
	worst = []

	for path in paths:
		# The number of PROGRESS.md edits in this transcript.
		edits = 0

		for record in records(path):
			if record.get("type") != "assistant":
				continue

			for name, params in tool_calls(record):
				if name in ("Edit", "Write") and str(
					params.get("file_path", "")
				).endswith("PROGRESS.md"):
					edits += 1

		if edits > 1:
			# The transcript modification date used for daily totals.
			day = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
				"%Y-%m-%d"
			)
			total += edits
			sessions += 1
			by_day[day][0] += edits
			by_day[day][1] += 1
			worst.append(
				(
					edits,
					os.path.basename(path)[:8],
					os.path.basename(os.path.dirname(path))[-34:],
				)
			)

	print(f"repeated PROGRESS.md edits: {total} across {sessions} sessions")
	print("\nby day (edits / sessions):")

	for day in sorted(by_day):
		print(f"  {day}  {by_day[day][0]:4d}  {by_day[day][1]}")

	print("\nworst sessions:")

	for edits, sid, project in sorted(worst, reverse=True)[:6]:
		print(f"  {edits:3d}  {sid}  {project}")


# --- Section 3: read-after-edit ---------------------------------------------


def count_pairs(sequence, window, by_type):
	"""Count edits followed by a read of the same path within `window` calls.

	@param  {list}  sequence
	    Ordered (tool name, file path) pairs for one session.
	@param  {int}  window
	    How many following calls count as "immediately after".
	@param  {Counter|None}  by_type
	    Optional counter to tally the edited file's type into.
	"""
	# The number of edits followed closely by a read of the same file.
	pairs = 0

	for index, (name, path_edited) in enumerate(sequence):
		if name not in ("Edit", "Write"):
			continue

		for later_name, later_path in sequence[index + 1 : index + 1 + window]:
			if later_name == "Read" and later_path == path_edited:
				pairs += 1

				if by_type is not None:
					# The edited path used to group pairs by file type.
					text = str(path_edited)
					by_type[
						"PROGRESS.md"
						if text.endswith("PROGRESS.md")
						else (os.path.splitext(text)[1] or "none")
					] += 1

				break

	return pairs


def read_after_edit(paths):
	section("Verification re-reads after editing", "F7")

	# The formatter notice the harness emits when a PostToolUse hook rewrites a
	# file. Its presence in the window is what disproves the tooling-gap reading.
	# Matched against the raw line, because the notice is not carried in a text
	# or tool_result block and a parsed-content search misses almost all of them.
	notice = "PostToolUse hook modified"

	# The main agent's edit and read pair counts, keyed by how many following calls were checked.
	windows = {3: 0, 5: 0}
	# The edited file types counted in the shorter window.
	by_type = collections.Counter()
	# The number of formatter notices found in the selected transcripts.
	notices = 0
	# The number of nearby re-reads that came after a formatter notice.
	after_notice = 0
	# The number of nearby re-reads with no formatter notice before them.
	without_notice = 0
	# The number of edit and read pairs found in delegated calls.
	sidechain_pairs = 0

	for path in paths:
		# The ordered tool calls and formatter notices in this transcript.
		sequence = []

		with open(path, errors="replace") as handle:
			for line in handle:
				if notice in line:
					sequence.append(("NOTICE", None, False))
					notices += 1

				try:
					# The parsed transcript record for this line.
					record = json.loads(line)
				except ValueError:
					continue

				if record.get("type") != "assistant":
					continue

				# Every tool call enters the sequence, not just file operations, so
				# that "within N calls" means what it says: a read that follows an
				# edit almost immediately, rather than one separated by a dozen
				# greps and builds.
				for name, params in tool_calls(record):
					sequence.append(
						(name, params.get("file_path"), bool(record.get("isSidechain")))
					)

		# Main-agent pairs are counted separately from delegated ones: the report
		# quotes the main-agent figure, since subagent transcripts are governed by
		# their own delegation packets.
		# The main-agent file calls used for the report's pair counts.
		main = [
			(name, path_edited)
			for name, path_edited, side in sequence
			if not side and name != "NOTICE"
		]
		# The delegated file calls counted separately from main-agent calls.
		delegated = [
			(name, path_edited)
			for name, path_edited, side in sequence
			if side and name != "NOTICE"
		]

		windows[3] += count_pairs(main, 3, by_type)
		windows[5] += count_pairs(main, 5, None)
		sidechain_pairs += count_pairs(delegated, 3, None)

		# Notices stay in the sequence here, so a re-read can be attributed to one.
		# Every call and notice in this transcript's sequence, without its sidechain flag.
		flat = [(name, path_edited) for name, path_edited, _ in sequence]

		for index, (name, path_edited) in enumerate(flat):
			if name not in ("Edit", "Write"):
				continue

			# The following calls checked for a nearby read or formatter notice.
			lookahead = flat[index + 1 : index + 6]
			# Whether a formatter notice appears among those following calls.
			saw_notice = any(item[0] == "NOTICE" for item in lookahead)

			for later_name, later_path in lookahead:
				if later_name == "Read" and later_path == path_edited:
					if saw_notice:
						after_notice += 1
					else:
						without_notice += 1

					break

	print(f"read-after-edit pairs, main agent (3-call window): {windows[3]}")
	print(f"read-after-edit pairs, main agent (5-call window): {windows[5]}")
	print(f"read-after-edit pairs, delegated agents (3-call window): {sidechain_pairs}")
	print(
		f"\nby file type, main agent (3-call window): {dict(by_type.most_common(10))}"
	)
	print(f"\nformatter notices in window: {notices}")
	print(f"  re-reads that followed a notice:  {after_notice}")
	print(f"  re-reads with no notice at all:   {without_notice}")


# --- Section 4: friction CLI ------------------------------------------------


def friction():
	"""Print post-cutoff friction figures returned by the friction CLI."""
	section("Friction CLI composition", "F8")

	# The friction command that lists the entries for the report.
	command = [
		"friction",
		"list",
		"--json",
		"--since",
		"2026-07-04",
		"--include-check-fails",
		"--include-tool-errors",
	]

	try:
		# The friction CLI result containing JSON event data.
		result = subprocess.run(command, capture_output=True, check=True, text=True)
		# The parsed friction CLI response.
		response = json.loads(result.stdout)
	except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
		print("friction CLI data unavailable")
		return

	# The friction event rows, when the response has the expected shape.
	rows = response.get("data") if isinstance(response, dict) else None
	if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
		print("friction CLI returned invalid JSON")
		return

	# The event counts grouped by friction category.
	categories = collections.Counter(row.get("category") for row in rows)
	# The failed-check events examined for missing error details.
	check_fails = [row for row in rows if row.get("category") == "check-fail"]
	# The failed-check events without an error summary.
	empty = [row for row in check_fails if not row.get("error")]
	# The failed-check counts grouped by repository directory.
	by_repo = collections.Counter(
		os.path.basename(str(row.get("cwd", ""))) for row in check_fails
	)
	# The failed-check counts grouped by minute to show retry clusters.
	per_minute = collections.Counter(
		str(row.get("timestamp_utc", ""))[:16] for row in check_fails
	)

	print(f"entries in window: {len(rows)}")
	print(f"by category: {dict(categories.most_common())}")
	print(
		f"\ncheck-fail rows with an empty error summary: {len(empty)} of {len(check_fails)}"
	)
	print(f"  as a share of all entries: {len(empty) / len(rows):.0%}")
	print(f"\ncheck-fail by repository: {dict(by_repo.most_common(5))}")
	print(f"\nbusiest minutes (retry clusters): {per_minute.most_common(5)}")


# --- Section 5: response verbosity ------------------------------------------


def verbosity(paths):
	section("Assistant response verbosity", "Not findings")

	# The lengths of assistant text blocks in selected sessions.
	lengths = []
	# The large text blocks retained as report examples.
	large = []

	for path in paths:
		for record in records(path):
			if record.get("type") != "assistant" or record.get("isSidechain"):
				continue

			for block in blocks(record):
				if block.get("type") != "text":
					continue

				# The assistant text measured for response length.
				text = block.get("text", "").strip()

				if not text:
					continue

				lengths.append(len(text))

				if len(text) > 4000:
					large.append(
						(
							len(text),
							os.path.basename(path)[:8],
							(record.get("timestamp") or "")[:16],
						)
					)

	lengths.sort()

	print(f"assistant text blocks: {len(lengths)}")
	print(f"  median {statistics.median(lengths):.0f}")
	print(f"  p90    {lengths[int(0.90 * len(lengths))]}")
	print(f"  p99    {lengths[int(0.99 * len(lengths))]}")
	print(f"  max    {lengths[-1]}")
	print(f"\nblocks over 4000 characters: {len(large)}")

	for size, sid, timestamp in sorted(large, reverse=True)[:5]:
		print(f"  {size:6d}  {sid}  {timestamp}")


# --- Section 6: command discipline ------------------------------------------


def commands(paths):
	"""Print the number of Bash commands, and how many ran raw npm or Vitest, bounded their output, or ran Playwright scoped to named tests or broadly."""
	section("Command discipline", "Successful patterns")

	# The Bash command counts grouped by report category.
	counts = collections.Counter()

	for path in paths:
		for record in records(path):
			if record.get("type") != "assistant":
				continue

			for name, params in tool_calls(record):
				if name != "Bash":
					continue

				# The Bash command text checked for execution patterns.
				command = str(params.get("command", ""))
				counts["bash total"] += 1
				# The command family used to count raw npm and Vitest calls.
				classification = classify_bash_command(command)

				if classification in ("npm test", "npm lint", "vitest"):
					counts["raw npm or vitest"] += 1

				if re.search(r"\|\s*(tail|head)\b", command):
					counts["output bounded"] += 1

				if re.search(r"test:component|playwright test|\.pw\.js", command):
					# Whether this Playwright call names a test file or selection.
					scoped = re.search(r"--test-file|\.pw\.js|--grep|-g ", command)
					counts["playwright scoped" if scoped else "playwright broad"] += 1

	for key in (
		"bash total",
		"raw npm or vitest",
		"output bounded",
		"playwright scoped",
		"playwright broad",
	):
		print(f"  {key:22s} {counts[key]}")


# --- Section 7: skill presence ----------------------------------------------


def skills(paths):
	section("code-style presence in source-editing sessions", "F5")

	# Skills load either through the Skill tool or by injection, which appears in
	# the transcript as a "Base directory for this skill" preamble.
	# The transcript marker used to recognise an injected skill.
	injected = re.compile(r"Base directory for this skill: \S+/([\w-]+)")

	# The number of sessions with at least three source edits.
	sessions = 0
	# The number of those sessions that loaded code-style.
	with_code_style = 0

	for path in paths:
		# The skills seen in this transcript through calls or injection.
		loaded = set()
		# The number of JavaScript, TypeScript and Vue file edits in this session.
		source_edits = 0

		for record in records(path):
			for name, params in (
				tool_calls(record) if record.get("type") == "assistant" else ()
			):
				if name == "Skill":
					loaded.add(params.get("skill"))
				elif name in ("Edit", "Write") and str(
					params.get("file_path", "")
				).endswith((".vue", ".js", ".ts")):
					source_edits += 1

			for match in injected.finditer(record_text(record)):
				loaded.add(match.group(1))

		if source_edits >= 3:
			sessions += 1

			if "code-style" in loaded:
				with_code_style += 1

	print(f"sessions editing 3 or more JS/Vue/TS files: {sessions}")
	print(f"  with code-style present: {with_code_style}")


def main():
	# The command-line parser for the report's time window.
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--days", type=int, default=21, help="window size in days (default 21)"
	)
	# The parsed command-line options, including the time window.
	args = parser.parse_args()

	# The transcript paths selected for this report.
	paths = session_paths(args.days)

	corpus(paths)
	progress_churn(paths)
	read_after_edit(paths)
	friction()
	verbosity(paths)
	commands(paths)
	skills(paths)


if __name__ == "__main__":
	main()
