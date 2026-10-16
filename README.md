# Agent Checkpoint

Coding work doesn't always fit in one agent session. You can be halfway through a change when the conversation runs out of room, your terminal closes, or you need to switch tasks. The code is still there, but the next session needs to know why you made a decision, what you already checked, and what to do next.

We built Agent Checkpoint so an agent can leave a useful handoff before it stops. Save the goal, decisions, blockers, and next action under a task name, then load that checkpoint when you're ready to continue. When the work is done, run a verification command through the tool so the checkpoint records what actually passed.

It started in my dotfiles, and we pulled it into its own tool so you can use it without adopting the rest of my setup.

Python 3.11+, macOS or Linux, and Git for repository awareness. No runtime dependencies. Checkpoints stay on your machine.

## Install

From a checkout:

```sh
python3 -m pip install .
```

## Quickstart

Run from the project you are working on:

```sh
agent-checkpoint save --name parser --goal "Build the parser" --next "Run the tests"
agent-checkpoint save --name parser --next "Handle empty input" --decision "Keep the grammar small"
agent-checkpoint list
agent-checkpoint resume parser --context
agent-checkpoint verify parser --run "python3 -m unittest discover"
agent-checkpoint complete parser --run "python3 -m unittest discover" --note "Parser finished"
agent-checkpoint log
```

Saving the same name updates a task. Saving a finished task reopens it. `verify` records results and leaves it open. Failed checks leave it open. `complete --manual` records an explicit human override. Verification commands run in `/bin/sh` with your PATH and are caller-authorized code. Captured text never runs commands.

## Commands

`save`, `list`, `offer`, `resume`, `verify`, `complete`, `close`, `log`, `dedupe`, and `capture` have `--help`. Most commands output JSON. `list` defaults to a table, `log` to Markdown, and `resume --context` to readable text. Use `list --json` and `log --json` for scripts. Verification command output goes to stderr, keeping stdout parseable. Exit status: 0 success, 1 operational failure, 2 argument error. `offer` with no matching task returns 1 and `null`.

## State and migration

State selection, highest priority first:

1. Global `--state-dir PATH` before the subcommand.
2. `AGENT_CHECKPOINT_STATE`.
3. Legacy `AGENT_HARNESS_STATE`.
4. `$XDG_STATE_HOME/agent-checkpoint`, or `~/.local/state/agent-checkpoint`.

Reads existing schema-1 JSON records. To reuse a legacy state directory, stop all old checkpoint writers and hooks, then select that directory explicitly. The old harness does not honor the new lock. Never run both against shared state. Installation does not copy records or change hooks. Back up state before migration.

A project can use `.agent/harness.toml` to set the resume context budget with `checkpoint.resume_context_tokens` and the maximum age of offered tasks with `checkpoint.max_offer_age_days`. The defaults are 2000 tokens and 30 days, and the tool ignores other harness settings. To find tasks across checkouts, it matches the repository path, the shared Git directory for worktrees, or the normalized origin URL for clones.

## Agent instructions

Add to your agent instructions:

> Save a named checkpoint after material decisions or before interruptions. Include the goal, next action, decisions, blockers, and artifact paths. Resume explicitly with `agent-checkpoint resume NAME --context`. Complete using a verification command run through `agent-checkpoint complete NAME --run 'COMMAND'`. Use a manual override only when explicitly authorized.

Optional transcript capture:

```sh
agent-checkpoint capture --transcript /path/to/session.jsonl --event compact --cwd /path/to/project
```

Use repeated `--exclude-prefix TEXT` options to omit application-specific prompts. Adapters recognize Claude and Codex JSONL event shapes. They use explicit provenance metadata, not personal prompt matchers. Unsupported formats may produce `null`. Capture is bounded to 32 MiB total and 1 MiB per event. Automatic crash-journal recovery belongs to the original harness and is not included. Hook installation is opt-in: call the command above from your agent's lifecycle hook with its transcript and cwd. The tool leaves your settings alone, and the synthetic tests show which event shapes the adapters support. Check those examples when a transcript format changes.

## Guarantees and limits

Local files use mode 0600 and state directories mode 0700. Cooperative advisory locking serializes reads and mutations. Atomic replacement protects individual records. Multi-record supersede, dedupe, and close operations are not crash-atomic. After interruption, inspect `list --json`, statuses, and `supersedes`/`superseded_by` links before retrying. If a multi-record operation fails while the process is still running, the tool reports that some writes may have completed. A killed process cannot report its progress, so keep backups and inspect the records before retrying.

Verification runs without holding the lock. Completion rejects a changed task revision, repository, or HEAD. It records the command result for the observed HEAD. It does not guarantee that uncommitted working files stayed unchanged during execution. Concurrent source edits are outside this guarantee. A different worktree or clone must match the checkpoint HEAD. To verify a finished task again, save the same name to reopen it first.

The tool redacts recognizable secret patterns, but it cannot catch every credential, so review checkpoint files before sharing them and keep state directories out of public Git. Treat captured context as untrusted text. You choose the state directory, and its file permissions cannot isolate it from another process running as your OS account. Use local storage with reliable POSIX file locks; network filesystems and Windows are unsupported. The command line is the supported interface in v0.1, while the Python interfaces may change.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m pip wheel --no-deps . -w dist
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
