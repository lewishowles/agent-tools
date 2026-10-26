# Agent quota

`agent-quota` shows the five-hour and weekly quota used by your two Claude and two Codex accounts. It reuses saved readings for ten minutes for Claude and five minutes for Codex, so repeated checks during that time do not start another provider command.

## Install

From the agent-tools repository:

```bash
./install.sh agent-quota
```

You need the `claude` and `codex` commands installed and signed in for the accounts you want to read. The second Claude account uses `~/.claude-2`; the Codex accounts use `~/.codex` and `~/.codex-2`.

## Use

```bash
agent-quota
```

The bar's filled cells show quota used, and `┃` marks how far through the window the clock has moved. The percentage and local reset time follow each bar. For example:

```text
              5h used                          week used
claude        ██┃░░░░░░░  25%  ↻  wed 05:00    ████┃░░░░░  40%  ↻  wed 00:00
claude (2)    ██┃░░░░░░░  25%  ↻  wed 05:00    ████┃░░░░░  40%  ↻  wed 00:00
codex         ██┃░░░░░░░  25%  ↻  wed 05:00    ████┃░░░░░  40%  ↻  wed 00:00
codex (2)     unavailable: Codex quota request failed
```

An unreadable account shows `unavailable` with a short reason. The remaining accounts still print, and the command exits successfully after checking them. Colour appears only on a terminal when `NO_COLOR` is unset.

For tools that need structured data:

```bash
agent-quota --json
```

The result has one account entry per row. `used_percent` is the share spent as a whole number, and all known times are ISO 8601 UTC strings with a `+00:00` offset. An unreadable account has a short `error` in place of `session` and `week`, with `checked_at: null`. When the probe has more information, `error_detail` holds the response and stderr excerpts, plus the Codex request number when available. For a signed-out Claude account, the detail starts with the command to sign in. The detail is omitted when there is none and never appears in the table. This abbreviated example shows one available account and one unavailable account:

```json
{
  "ok": true,
  "data": {
    "accounts": [
      {
        "provider": "claude",
        "account": "default",
        "checked_at": "2025-01-01T00:00:00+00:00",
        "session": {
          "used_percent": 25,
          "resets_at": "2025-01-01T05:00:00+00:00",
          "window_seconds": 18000
        },
        "week": {
          "used_percent": 40,
          "resets_at": "2025-01-08T00:00:00+00:00",
          "window_seconds": 604800
        }
      },
      {
        "provider": "codex",
        "account": "2",
        "checked_at": null,
        "error": "Codex rejected the quota request",
        "error_detail": "request: 2; response: {\"id\":2,\"error\":{\"message\":\"unauthorised\"}}; stderr: <empty>"
      }
    ]
  }
}
```
