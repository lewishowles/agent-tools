# Agent board

`agent-board` keeps a live view of HCOM teams in one terminal pane. It refreshes every two seconds and fills the pane's width with a rounded frame. The top edge shows the current time; the bottom edge counts teams waiting on you and working. Teams that need you appear above working teams, with blocked teams at the top. Each row shows a status symbol and status before the team name. Working teams show whether they are implementing, reviewing, checking, coordinating, starting, or working. Blocked teams show how long they have been blocked, and a lone working agent has `(partial team)` after the team name. Team status comes from `hcom list --json`; the board does not read conversations.

```text
╭─ ✻ agent board ─────────────────────────────── 12:34:56 ─╮
│                                                          │
│  WAITING ON YOU                                          │
│  ✕ blocked · 4m  agent-tools                             │
│  ● needs you     Lew-Timer                               │
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
