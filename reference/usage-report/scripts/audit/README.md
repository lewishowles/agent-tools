# Session audit scripts

Read-only analysis over Claude Code and Codex transcripts. The scripts never write to transcripts.
Generated reports and working data belong under `.agent/audits/`; `token_usage_report.py` overwrites its three
fixed report files under `.agent/audits/usage/`.

Transcripts live at `~/.claude/projects/<encoded-project-path>/<session-id>.jsonl`, one JSON record
per line. The record types these scripts care about are `user` and `assistant`; both carry a
`message.content` list whose blocks are `text`, `tool_use`, or `tool_result`. Records with
`isSidechain: true` come from delegated subagents in the same transcript. Agents launched through
hcom write their own separate transcripts, so they appear as sessions rather than sidechains.

All scripts default to a 21-day window measured from when they run, so figures drift as the window
moves. Python 3 only, no dependencies.

## token_usage_report.py

Aggregates token usage for Claude and Codex over a bounded UTC window. The report is deliberately
mechanical: it reports facts and does not make cost-reduction recommendations. Every figure is
tokens, not cost. It includes totals by tool, model, day, project directory, and hcom role; the
ten heaviest sessions; Claude cache-read ratio; and Codex reasoning-output ratio.
It also includes ranked Driver ledger sections for aggregate and per-session bash, file, skill,
hook, MCP, and other tool activity, with safe targets, payload estimates, failures, retries, and
repetition markers.

```sh
python3 scripts/audit/token_usage_report.py --days 7
python3 scripts/audit/token_usage_report.py --since 2026-08-01 --until 2026-08-06
```

Set `CLAUDE_CONFIG_DIR` or `CODEX_HOME` to read transcripts from a non-default location; both
fall back to `~/.claude` and `~/.codex` respectively.

Both runs overwrite `.agent/audits/usage/latest.md` (compact session summary),
`.agent/audits/usage/latest-detail.md` (full driver-ledger detail), and
`.agent/audits/usage/latest.json`. The explicit date form uses inclusive UTC calendar dates and
is byte-stable when repeated with the same bounds, provided the window has fully elapsed — a
window that includes the current moment (e.g. today, or `--days 1`) will differ between runs as
live sessions keep appending. A missing hcom database or an unmatched transcript is labelled
`unattributed`.

The Codex `event_msg` token-count payload stores `total_token_usage` cumulatively for the session.
`last_token_usage` is the per-event delta. This was confirmed against a real session containing
ten consecutive token-count events, where the second cumulative total was the first total plus
the second event's `last_token_usage`. The script sums `last_token_usage`, deriving a delta from
consecutive cumulative totals only when the per-event value is absent. Codex rollouts are read
from both `~/.codex/sessions/` and `~/.codex/archived_sessions/`.

## metrics.py

Reproduces every aggregate count quoted in the audit report, grouped by the finding it supports.
Start here.

```
python3 scripts/audit/metrics.py [--days 21]
```

Covers corpus size, `PROGRESS.md` write churn by day, read-after-edit pairs and their relationship
to formatter notices, friction log composition, response-length distribution, command discipline,
and `code-style` skill presence in source-editing sessions.

Two details worth knowing before trusting a modified copy:

- The formatter notice (`PostToolUse hook modified …`) is matched against the raw transcript line,
  not against parsed message content. It does not appear in a `text` or `tool_result` block, so a
  content-based search finds almost none of them. An earlier version of this analysis made that
  mistake and reached the opposite conclusion about F7.
- The read-after-edit window counts _all_ tool calls, not just file operations. Counting only file
  operations inflates the figure by roughly 30%, because it treats a read separated from its edit by
  a dozen greps and builds as "immediately after".

## redundancy.py

Flags three redundancy patterns across all sessions: identical Bash commands repeated four or more
times, a file read immediately after being edited, and a file read six or more times.

```
python3 scripts/audit/redundancy.py
```

Its read-after-edit count is stricter than `metrics.py` because it excludes sidechain records. Where
the two disagree, `metrics.py` is the one the report quotes.

A caution the audit ran into: repetition is not automatically waste. Most repeated diagnostics
commands in the output turned out to be a lint run after each edit, which is correct behaviour.
Check what happened between the repeats before treating any of these as a finding.
