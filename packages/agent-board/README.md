# Agent board

`agent-board` keeps a live view of HCOM teams in one terminal pane. It refreshes every two seconds and shows teams that are blocked, partly running, or idle above a single quiet-team line. Only blocked teams show how long they have been blocked. Team status comes from `hcom list --json`; the board does not read conversations.

Install it from this workspace:

```sh
uv tool install --from packages/agent-board agent-board
```

Run `agent-board` beside your HCOM teams. Press Ctrl+C to close it. The board needs `hcom` on `PATH`.
