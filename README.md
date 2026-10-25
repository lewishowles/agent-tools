# Agent tools

An agent is an AI assistant, such as Claude Code or Codex, that can read files and run commands on your behalf. Agents work best with short, predictable command output and a written record of what has been done. Without those, they waste effort rereading long output or lose track of earlier work. These command-line tools fill those gaps: they run project commands with short output, track tasks across sessions, pass messages between agents working as a team, and turn web pages and images into a smaller form an agent can read with less of its limited context.

The tools are for developers who use agents in their own projects. Each tool installs on its own, so you only need the ones you want.

## Requirements

- Every tool except `retina-shrink` needs Python 3.11 or newer and [uv](https://docs.astral.sh/uv/getting-started/installation/) for installation.
- Run `agent-run`, `progress`, and `review-feedback` inside a Git repository. `review-feedback` needs a Git worktree; `agent-run` and `progress` use Git to tell which project you are in.
- `agent-quota` needs the `claude` and `codex` commands installed and signed in for the accounts you want to read.
- `agent-board` and `team-send` need an active [HCOM](#glossary) session. [Install HCOM separately](https://github.com/aannoo/hcom#install); these instructions do not install it. `team-send` also needs `hcom` on your `PATH`.
- `page-to-markdown --copy` and the clipboard features in `review-feedback` need macOS.
- `retina-shrink` needs macOS 13 or newer and Swift 6.0 or newer. It is a Swift package, so the Python installer does not include it.

## Tools

| Tool                                                    | What it does                                                                                     | Special requirement                                  |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| [agent-board](packages/agent-board/README.md)           | Shows HCOM teams that need your attention in one terminal board                                  | HCOM session                                         |
| [agent-quota](packages/agent-quota/README.md)           | Shows Claude and Codex quota for each account                                                    | `claude` and `codex` installed and signed in         |
| [agent-run](packages/agent-run/README.md)               | Runs project commands with short output                                                          | Run inside a Git repository                          |
| [doc-paths](packages/doc-paths/README.md)               | Checks local paths named in Markdown files                                                       | None                                                 |
| [friction](packages/friction/README.md)                 | Records times an agent ignored a rule or got stuck, so you can spot repeat problems              | None                                                 |
| [page-to-markdown](packages/page-to-markdown/README.md) | Turns a URL, local HTML file, or piped HTML into compact Markdown                                | macOS for `--copy`                                   |
| [progress](packages/progress/README.md)                 | Tracks local project releases, tasks, chunks, and handoffs                                       | Run inside a Git repository                          |
| [review-feedback](packages/review-feedback/README.md)   | Prepares review comments and reviewable patches; also installs `review-patches`                  | Git worktree; macOS for clipboard input and `--copy` |
| [team-send](packages/team-send/README.md)               | Sends an HCOM message to one live teammate by role                                               | HCOM session and `hcom` on `PATH`                    |
| [retina-shrink](packages/retina-shrink/README.md)       | Makes copied images smaller before chat and halves Retina image files before an agent opens them | macOS 13+, Swift 6.0 or newer; install separately    |

## Install, upgrade, or remove a tool

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and have Python 3.11 or newer available before installing a Python tool. The installer offers the nine Python tools. Run this command in a terminal to choose from a menu:

```sh
curl -fsSL https://raw.githubusercontent.com/lewishowles/agent-tools/main/install.sh | bash
```

Enter one or more names or menu numbers, separated by spaces or commas. If you have cloned this repository, you can make the same choice with `./install.sh` or pass a name, a number, or `--all` directly:

```sh
./install.sh doc-paths
./install.sh 4  # 4 is doc-paths in the menu
./install.sh --all
```

To check that a tool installed, run it with `--help`, such as `doc-paths --help`. If your shell cannot find the command, run `uv tool update-shell` and open a new terminal. Run the installer again to upgrade a tool: it reinstalls the latest version from the GitHub `main` branch. Remove a Python tool with `uv tool uninstall <tool-name>`, such as:

```sh
uv tool uninstall doc-paths
```

If you prefer to install a Python tool without the menu, use uv with its package directory. Replace `<tool-name>` with a name from the table above:

```sh
uv tool install "git+https://github.com/lewishowles/agent-tools.git#subdirectory=packages/<tool-name>"
```

`retina-shrink` is built with Swift rather than Python, so `install.sh` and `uv tool install` do not install it. Follow its [build and install instructions](packages/retina-shrink/README.md#build-and-install).

## Glossary

| Term                 | Meaning                                                                                                  |
| -------------------- | -------------------------------------------------------------------------------------------------------- |
| Agent                | An AI assistant that can work with files and commands for you.                                           |
| HCOM                 | A messaging tool that lets agents working as a team send each other messages and see who is active.      |
| Orchestrator         | The agent that coordinates a team and brings the result back to you.                                     |
| Scout                | The agent that gathers facts and checks the project state.                                               |
| Implementer          | The agent that makes the requested changes.                                                              |
| Reviewer             | The agent that checks a change and reports what still needs attention.                                   |
| Release              | A group of related project tasks that `progress` tracks together.                                        |
| Task                 | A piece of project work with a stated outcome, tracked by `progress`.                                    |
| Chunk                | One reviewable part of a task, tracked by `progress`.                                                    |
| Handoff              | A note `progress` tracks to tell the next agent what has happened and what to do next.                   |
| Command registration | A project command, such as a test or build command, saved in `agent-run` so an agent can run it by name. |
| Editable install     | An install that uses your local source files directly, so source edits take effect without reinstalling. |
| Sandbox              | A restricted environment that limits what a command can access or change.                                |

## Contributing

### Package layout

The root `pyproject.toml` is a virtual uv workspace. It discovers Python packages under `packages/`, but it is not a package itself. `packages/retina-shrink` is a Swift package excluded from the uv workspace.

Each Python package owns its own:

- `pyproject.toml`
- version
- dependencies
- command-line entry points
- source code and tests

Packages are independent. The repository does not provide a shared application framework, base class, or version.

```text
packages/
└── <package-name>/
    └── pyproject.toml
```

### Add an independent Python package

1. Create `packages/<package-name>/`.
2. Add a package-owned `pyproject.toml` with the package name, version, dependencies, and command-line entry points.
3. Follow the [agent-facing CLI contract](docs/cli-contract.md) for output, errors, exit codes, truncation, and evidence IDs.
4. Add the package's source code and tests inside its directory.
5. Run `uv lock --dry-run` from the repository root to confirm the workspace finds the package.
