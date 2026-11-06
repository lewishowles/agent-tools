# Team send

`team-send` sends a message to one live HCOM teammate by role. It uses your `HCOM_TAG` to find your team or a named team in the same repository, sends to the chosen agent's exact name, and checks HCOM's delivery receipt before reporting success.

## Install

From the agent-tools workspace, install the command with:

```sh
uv tool install --from packages/team-send team-send
```

You'll also need `hcom` on `PATH` and the `HCOM_TAG` and `HCOM_NAME` variables from your HCOM session.

## Usage

Pass a message file with `--body-file <path>`. After HCOM confirms delivery, `team-send` removes the file if it has not changed and leaves its folder in place. Make a new file for each message. Roles are `orchestrator`, `scout`, `implementer`, and `reviewer`. An intent is required:

```sh
report_file=$(mktemp)
printf 'The check passed.\n' > "$report_file"
team-send scout --intent inform --body-file "$report_file"
```

`--body-file` is required; `team-send` never reads stdin. It refuses a missing, empty, or unreadable file, and anything that isn't an ordinary file, such as a folder or symlink. A missing file may already have been sent; write a new one. A failed or unconfirmed send leaves the file in place. If HCOM times out during a send, the refusal says the send timed out and that the message may still have been delivered. If HCOM could not start the send, the refusal says `HCOM send failed` without suggesting delivery. An unconfirmed delivery receipt also means the message may still have been delivered. If delivery is confirmed but the file has changed since it was read, the command succeeds, keeps the file, and warns that its current contents have not been sent. If delivery is confirmed but removal fails, the command exits successfully and warns you not to send the file again.

Use `--team <label>` to send to a role in a named team of the same repository. A label uses lowercase letters, numbers and hyphens, with no hyphen at the start or end. `team-send` finds your repository by removing your `HCOM_TEAM_LABEL` from just before the role in your `HCOM_TAG`, and refuses if the label isn't there. An empty `HCOM_TEAM_LABEL` counts as no label. Without `--team`, the command sends within your current team.

`--reply-to` and `--thread` are passed to HCOM. An `ack` intent requires `--reply-to`. Add `--json` for a machine-readable `ok`/`data` success envelope or an `ok`/`error` failure envelope. When a send succeeds but its file could not be removed, `--json` adds a `warning` string to `data`, and plain output prints the warning on stderr.

The command exits with status `0` only when the receipt names the one chosen teammate. It exits with status `1` if `--body-file` is missing, the body is empty, the body file cannot be used, the sender, role or team label is invalid, your `HCOM_TAG` doesn't have your `HCOM_TEAM_LABEL` just before the role, HCOM cannot list or send, the role has no single live teammate, or the delivery receipt cannot confirm exactly one recipient. It never retries a send. Invalid command arguments exit with status `2`.
