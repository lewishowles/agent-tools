# Agent board

`agent-board` keeps a live view of HCOM teams in one terminal pane. It refreshes every two seconds and draws a left-aligned rounded frame up to 80 columns wide. The top edge shows the current time; the bottom edge counts teams waiting on you and working. Teams that need you appear above working teams, with blocked teams at the top. Each row shows a status symbol and status before the team name. Working teams show whether they are implementing, reviewing, checking, coordinating, starting, or working. A team shows `stuck` when nobody is active, blocked, or launching and at least one listening member has had unread messages for more than 60 seconds, or when a team with an active member has had no new HCOM event for five minutes. This also happens while a member runs one long tool call, such as a test run or build, because no events arrive until it finishes. A quiet team can show a reason such as `stuck · model at capacity` when the active member's terminal shows a known failure. Stuck teams appear with the teams that need you. A team shows `stale` when an active member's HCOM session no longer matches its transcript, which happens when HCOM moves the agent onto a background session that stops updating; stale teams appear below blocked teams and above stuck ones. Blocked teams show how long they have been blocked, and a lone working agent has `(partial team)` after the team name. Team status starts with `hcom list --json`; the board tracks unread time between refreshes, checks active teams' latest events at most every 30 seconds, and reads a quiet member's terminal once. It does not read conversations.

Apart from blocked teams, each row shows how long the team has been in its current status. A `+` means the team was already in that status when the board first saw it. The timer resets when the status changes or the board restarts. A stuck team's time counts from when it went quiet: its last HCOM event, or when the board first saw its unread messages.

Below 30 columns, the board shows plain rows without the frame, headings, clock, or counts, and shortens long rows to fit.

The frame turns red for blocked teams, magenta for teams that need you, and amber otherwise; set `NO_COLOR` to show the same board without colour.

```text
╭─ ✻ agent board ─────────────────────────────── 12:34:56 ─╮
│                                                          │
│  WAITING ON YOU                                          │
│  ✕ blocked · 4m       agent-tools                        │
│  ● needs you · 2m+    Lew-Timer                          │
│                                                          │
│  WORKING                                                 │
│  ▶ implementing · 1m  agent-tools · agent-board          │
│  ◌ checking · 30s     agent-tools · scratch              │
│                                                          │
╰─ 2 waiting on you · 2 working ───────────────────────────╯
```

Install it from this workspace:

```sh
uv tool install --from packages/agent-board agent-board
```

Run `agent-board` beside your HCOM teams. Press Ctrl+C to close it. The board needs `hcom` on `PATH`.
