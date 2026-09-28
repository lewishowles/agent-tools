# Agent board

`agent-board` keeps a live view of HCOM teams in one terminal pane. It refreshes every two seconds and lists the teams that need you, blocked ones first, above working teams. Blocked teams show how long they have been blocked, and a lone working agent is marked as a partial team. Team status comes from `hcom list --json`; the board does not read conversations.

Install it from this workspace:

```sh
uv tool install --from packages/agent-board agent-board
```

Run `agent-board` beside your HCOM teams. Press Ctrl+C to close it. The board needs `hcom` on `PATH`.
