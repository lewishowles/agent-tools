#!/usr/bin/env python3
"""Detect redundant tool patterns: identical repeated bash, re-read after edit, repeated reads."""

import collections
import datetime
import glob
import json
import os

from metrics import COMMAND_TEXT_LIMIT

# The Claude project directory containing session transcripts.
ROOT = os.path.expanduser("~/.claude/projects")


def repeated_read_indexes(calls):
	"""Return Read indexes for files read at least six times.

	@param  {list}  calls
		Ordered ``(name, input, timestamp)`` tuples for one session.
	@return {set}
		Indexes covered by the repeated-read threshold.
	"""
	# The positions of Read calls grouped by file path.
	read_indexes = collections.defaultdict(list)
	for index, (name, tool_input, _) in enumerate(calls):
		if name == "Read" and tool_input.get("file_path"):
			read_indexes[tool_input["file_path"]].append(index)

	# The positions of reads for files meeting the repeat threshold.
	repeated = set()
	for indexes in read_indexes.values():
		if len(indexes) >= 6:
			repeated.update(indexes)

	return repeated


def repeated_call_indexes(calls):
	"""Return call indexes covered by the existing redundancy thresholds.

	@param  {list}  calls
		Ordered ``(name, input, timestamp)`` tuples for one session.
	@return {set}
		Indexes marked as repeated Bash, repeated Read, or read-after-edit.
	"""
	# The positions of tool calls that meet a redundancy threshold.
	repeated = set()

	# The positions of Bash calls grouped by command text.
	bash_indexes = collections.defaultdict(list)
	for index, (name, tool_input, _) in enumerate(calls):
		if name == "Bash":
			# The command text, cut to COMMAND_TEXT_LIMIT characters, used to find repeated calls.
			command = str(tool_input.get("command", ""))[:COMMAND_TEXT_LIMIT]
			if command.strip():
				bash_indexes[command].append(index)

	for indexes in bash_indexes.values():
		if len(indexes) >= 4:
			repeated.update(indexes)

	repeated.update(repeated_read_indexes(calls))

	for index, (name, tool_input, _) in enumerate(calls):
		if name not in ("Edit", "Write"):
			continue

		# The file changed by this tool call, used to find a read of the same file within the next three calls.
		file_path = tool_input.get("file_path")
		for read_index, (later_name, later_input, _) in enumerate(
			calls[index + 1 : index + 4],
			start=index + 1,
		):
			if later_name == "Read" and later_input.get("file_path") == file_path:
				repeated.update((index, read_index))
				break

	return repeated


def main():
	"""Print the existing redundancy report for recent Claude sessions."""
	# The earliest transcript modification time included in the report.
	cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=21)
	# The occurrence counts for each redundant call pattern.
	agg = collections.Counter()
	# The sample calls retained for each redundant pattern.
	examples = collections.defaultdict(list)

	for path in glob.glob(os.path.join(ROOT, "*", "*.jsonl")):
		# The transcript metadata used to check its modification time.
		st = os.stat(path)
		if datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc) < cutoff:
			continue
		# The assistant tool calls found in this transcript.
		calls = []
		with open(path, errors="replace") as handle:
			for line in handle:
				try:
					# The parsed transcript record for this line.
					record = json.loads(line)
				except (TypeError, ValueError):
					continue
				if record.get("type") != "assistant" or record.get("isSidechain"):
					continue
				for block in record.get("message", {}).get("content", []) or []:
					if isinstance(block, dict) and block.get("type") == "tool_use":
						calls.append(
							(
								block.get("name"),
								block.get("input") or {},
								(record.get("timestamp") or "")[:19],
							)
						)

		# The shortened session ID shown with each example.
		session_id = os.path.basename(path)[:8]
		# The shortened project directory name shown with each example.
		project = os.path.basename(os.path.dirname(path))[-32:]
		# The positions of calls that meet a redundancy threshold.
		repeated = repeated_call_indexes(calls)

		# The repeated Bash calls grouped by command text.
		repeated_bash = collections.defaultdict(list)
		# The repeated Read calls grouped by file path.
		repeated_reads = collections.defaultdict(list)
		for index in repeated:
			# The call name, arguments, and unused timestamp at this position.
			name, tool_input, _ = calls[index]
			if name == "Bash":
				# The command text, cut to COMMAND_TEXT_LIMIT characters, used to group repeated calls.
				command = str(tool_input.get("command", ""))[:COMMAND_TEXT_LIMIT]
				if command.strip():
					repeated_bash[command].append(index)
		for index in repeated_read_indexes(calls):
			name, tool_input, _ = calls[index]
			if name == "Read" and tool_input.get("file_path"):
				repeated_reads[tool_input["file_path"]].append(index)

		for command, indexes in repeated_bash.items():
			agg["repeat_bash"] += 1
			examples["repeat_bash"].append(
				(len(indexes), project, session_id, command[:120])
			)

		for index in repeated:
			# The call details and timestamp used to describe a read after an edit.
			name, tool_input, timestamp = calls[index]
			if name in ("Edit", "Write"):
				for read_name, read_input, _ in calls[index + 1 : index + 4]:
					if read_name == "Read" and read_input.get(
						"file_path"
					) == tool_input.get("file_path"):
						agg["read_after_edit"] += 1
						examples["read_after_edit"].append(
							(
								project,
								session_id,
								timestamp,
								str(tool_input.get("file_path"))[-60:],
							)
						)
						break

		for file_path, indexes in repeated_reads.items():
			agg["repeat_read"] += 1
			examples["repeat_read"].append(
				(len(indexes), project, session_id, str(file_path)[-70:])
			)

	print(dict(agg))
	for key in examples:
		print(f"\n=== {key} (top) ===")
		# The sample rows to order and display for this pattern.
		example_rows = examples[key]
		if key in ("repeat_bash", "repeat_read"):
			example_rows.sort(key=lambda row: -row[0])
		for example in example_rows[:14]:
			print("  ", example)


if __name__ == "__main__":
	main()
