# Agent-facing CLI contract

Each package provides readable text by default and structured output through
`--json`. The package owns command-specific data and text layout. The following
behaviour is shared across packages.

## Output modes

The default output is human-readable text. Packages choose the headings,
tables, and command-specific wording.

With `--json`, a command writes one complete JSON document to standard output.
Progress and diagnostic messages go to standard error in this mode. When the
arguments include `--json`, a usage error found while parsing the other
arguments still uses the JSON envelope. Command-specific result fields belong
inside `data`.

## JSON envelope

A successful command returns `ok: true` and a `data` value:

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "name": "example",
        "status": "ready"
      }
    ]
  }
}
```

A command that cannot produce a successful result returns `ok: false` and an
error with one of the error codes below:

```json
{
  "ok": false,
  "error": {
    "code": "not-found",
    "message": "No item named \"example\" was found."
  }
}
```

`ok` is always a Boolean. `data` is present only for success, and `error` is
present only for failure. An error contains exactly `code` and `message`.
Only the codes in the error table are valid.

## Exit codes

| Code | Name              | Meaning                                                                                     |
| ---: | ----------------- | ------------------------------------------------------------------------------------------- |
|  `0` | success           | The command completed successfully                                                          |
|  `1` | result failure    | The command ran and found a problem                                                         |
|  `2` | usage error       | The command received invalid options or input                                               |
|  `3` | execution failure | The command could not complete because of its environment or an unexpected internal failure |

Packages use these codes consistently in text and JSON modes. They do not add
package-specific exit codes to this shared contract.

## Error codes

| Code           | Use when                                                                            | Exit code |
| -------------- | ----------------------------------------------------------------------------------- | --------: |
| `usage`        | An option, argument, or identifier has invalid syntax or is missing                 |       `2` |
| `not-found`    | The request is valid, but no matching resource or evidence exists                   |       `1` |
| `check-failed` | The command completed a check and the check result failed                           |       `1` |
| `environment`  | A required file, service, permission, or other external prerequisite is unavailable |       `3` |
| `internal`     | An unexpected failure prevents the command from completing                          |       `3` |

The `message` explains the specific problem in plain language. Callers branch
on the error code and exit code, not on the message text.

## Truncated output

When a result is intentionally shortened, the JSON `data` object includes
`truncated: true`, the number of visible entries in `shown`, the complete
entry count in `total`, and an absolute `log_path` for the full output:

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "name": "first"
      },
      {
        "name": "second"
      }
    ],
    "truncated": true,
    "shown": 2,
    "total": 5,
    "log_path": "/tmp/agent-tool/run-123.log"
  }
}
```

A result that is not truncated omits `truncated`, `shown`, `total`, and
`log_path`.

`shown` and `total` are non-negative integers, and `shown` is less than
`total`. The log path points to the complete output for that invocation.

Text output ends with one line in this form:

```text
Output truncated: showing 2 of 5 entries. Full output: /tmp/agent-tool/run-123.log
```

The notice is present whenever text output is truncated. Packages may choose
the visible content and the word used for the command's entries, but the notice
must remain one line and include the shown count, total count, and full log
path.

## Evidence IDs

An evidence ID has this fixed format:

```text
evd_<26 uppercase Crockford Base32 characters>
```

For example: `evd_01J8Q3Y7N0A2B4C6D8E0F1G2H3`.

Evidence IDs are opaque to callers. Once issued, an ID is immutable and is not
reused for another evidence record. Callers must not derive meaning from its
characters.

A validly formatted ID with no matching evidence returns the `not-found`
error code and exit code `1`. An ID with invalid syntax returns the `usage`
error code and exit code `2`.

## Package ownership

This contract defines behaviour at the command boundary. Packages own their
implementation, command-specific data, text presentation, and contract tests.
The repository does not require a shared library, base class, schema file,
plugin system, or fields that tell the caller which command to run next.
