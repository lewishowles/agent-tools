# Agent board

`agent-board` keeps a live view of HCOM teams in one terminal pane. It refreshes every two seconds and lists the teams that need you, blocked ones first, above working teams. Each row shows a status symbol and status before the team name. Working teams show whether they are implementing, reviewing, checking, coordinating, starting, or working. Blocked teams show how long they have been blocked, and a lone working agent has `(partial team)` after the team name. Team status comes from `hcom list --json`; the board does not read conversations.

```text
● needs you     Lew-Timer
✕ blocked · 4m  agent-tools

▶ implementing  agent-tools · agent-board
◌ checking      agent-tools · scratch
```

Install it from this workspace:

```sh
uv tool install --from packages/agent-board agent-board
```

Run `agent-board` beside your HCOM teams. Press Ctrl+C to close it. The board needs `hcom` on `PATH`.
