# Agent board

`agent-board` keeps a live view of HCOM teams in one terminal pane. It refreshes every two seconds and draws a left-aligned rounded frame up to 80 columns wide. The top edge shows the current time; the bottom edge counts teams waiting on you and working. Teams that need you appear above working teams, with blocked teams at the top. Each row shows a status symbol and status before the team name. Working teams show whether they are implementing, reviewing, checking, coordinating, starting, or working. A team shows `stuck` when nobody is active, blocked, or launching and at least one listening member has had unread messages for more than 60 seconds; it then appears with the teams that need you. A team shows `stale` when an active member's HCOM session no longer matches its transcript, which happens when HCOM moves the agent onto a background session that stops updating; stale teams appear below blocked teams and above stuck ones. Blocked teams show how long they have been blocked, and a lone working agent has `(partial team)` after the team name. Team status comes from `hcom list --json`; the board tracks unread time between refreshes and does not read conversations.

Below 30 columns, the board shows plain rows without the frame, headings, clock, or counts, and shortens long rows to fit.

The frame turns red for blocked teams, magenta for teams that need you, and amber otherwise; set `NO_COLOR` to show the same board without colour.

```text
╭─ ✻ agent board ─────────────────────────────── 12:34:56 ─╮
│                                                          │
│  WAITING ON YOU                                          │
│  ✕ blocked · 4m  agent-tools                             │
│  ● needs you     Lew-Timer                               │
│                                                          │
│  WORKING                                                 │
│  ▶ implementing  agent-tools · agent-board               │
│  ◌ checking      agent-tools · scratch                   │
│                                                          │
╰─ 2 waiting on you · 2 working ───────────────────────────╯
```

Install it from this workspace:

```sh
uv tool install --from packages/agent-board agent-board
```

Run `agent-board` beside your HCOM teams. Press Ctrl+C to close it. The board needs `hcom` on `PATH`.
