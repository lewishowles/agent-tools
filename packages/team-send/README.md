# Team send

`team-send` sends a message to one live HCOM teammate by role. It uses your `HCOM_TAG` to find your team or a named team in the same repository, sends to the chosen agent's exact name, and checks HCOM's delivery receipt before reporting success.

## Install

From the agent-tools workspace, install the command with:

```sh
uv tool install --from packages/team-send team-send
```

You'll also need `hcom` on `PATH` and the `HCOM_TAG` and `HCOM_NAME` variables from your HCOM session.

## Usage

Pass a message file with `--body-file <path>`. After HCOM confirms delivery, `team-send` removes that file and leaves its folder in place. Make a new file for each message. Roles are `orchestrator`, `scout`, `implementer`, and `reviewer`. An intent is required:

```sh
report_file=$(mktemp)
printf 'The check passed.\n' > "$report_file"
team-send scout --intent inform --body-file "$report_file"
```

Piped stdin also works for now:

```sh
printf 'Please review this change.\n' | team-send reviewer --intent request --thread review
printf 'Received.\n' | team-send orchestrator --intent ack --reply-to 42
printf 'The review is ready.\n' | team-send reviewer --team insights --intent inform
```

When `--body-file` is given, `team-send` ignores stdin. It refuses a missing, empty, or unreadable file, and anything that isn't an ordinary file, such as a folder or symlink. A missing file may already have been sent; write a new one. A failed or unconfirmed send leaves the file in place. If delivery is confirmed but removal fails, the command exits successfully and warns you not to send the file again.

Use `--team <label>` to send to a role in a named team of the same repository. A label uses lowercase letters, numbers and hyphens, with no hyphen at the start or end. `team-send` finds your repository by removing your `HCOM_TEAM_LABEL` from just before the role in your `HCOM_TAG`, and refuses if the label isn't there. An empty `HCOM_TEAM_LABEL` counts as no label. Without `--team`, the command sends within your current team.

`--reply-to` and `--thread` are passed to HCOM. An `ack` intent requires `--reply-to`. Add `--json` for a machine-readable `ok`/`data` success envelope or an `ok`/`error` failure envelope. When a send succeeds but its file could not be removed, `--json` adds a `warning` string to `data`, and plain output prints the warning on stderr.

The command exits with status `0` only when the receipt names the one chosen teammate. It exits with status `1` if the body is empty, the body file cannot be used, stdin is a terminal without `--body-file`, the sender, role or team label is invalid, your `HCOM_TAG` doesn't have your `HCOM_TEAM_LABEL` just before the role, HCOM cannot list or send, the role has no single live teammate, or the delivery receipt cannot confirm exactly one recipient. It never retries a send. Invalid command arguments exit with status `2`.
