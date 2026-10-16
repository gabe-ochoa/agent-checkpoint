# Agent Checkpoint

Save the goal, decisions, blockers, and next action for a coding task. Resume it in another session. Finish with a recorded verification command.

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

`.agent/harness.toml` in the project can set only `checkpoint.resume_context_tokens` and `checkpoint.max_offer_age_days`. Defaults are 2000 and 30. Other harness settings are ignored. Repository matching uses the checkout path, shared Git directory for worktrees, or normalized origin for clones.

## Agent instructions

Add to your agent instructions:

> Save a named checkpoint after material decisions or before interruptions. Include the goal, next action, decisions, blockers, and artifact paths. Resume explicitly with `agent-checkpoint resume NAME --context`. Complete using a verification command run through `agent-checkpoint complete NAME --run 'COMMAND'`. Use a manual override only when explicitly authorized.

Optional transcript capture:

```sh
agent-checkpoint capture --transcript /path/to/session.jsonl --event compact --cwd /path/to/project
```

Use repeated `--exclude-prefix TEXT` options to omit application-specific prompts. Adapters recognize Claude and Codex JSONL event shapes. They use explicit provenance metadata, not personal prompt matchers. Unsupported formats may produce `null`. Capture is bounded to 32 MiB total and 1 MiB per event. Automatic crash-journal recovery belongs to the original harness and is not included. Hook installation is opt-in: call the command above from your agent's lifecycle hook with its transcript and cwd. No settings are overwritten. Transcript formats can change. See the synthetic tests for supported shapes.

## Guarantees and limits

Local files use mode 0600 and state directories mode 0700. Cooperative advisory locking serializes reads and mutations. Atomic replacement protects individual records. Multi-record supersede, dedupe, and close operations are not crash-atomic. After interruption, inspect `list --json`, statuses, and `supersedes`/`superseded_by` links before retrying. Catchable failures during multi-record operations report possible partial progress. Abrupt termination cannot report progress. Keep backups.

Verification runs without holding the lock. Completion rejects a changed task revision, repository, or HEAD. It records the command result for the observed HEAD. It does not guarantee that uncommitted working files stayed unchanged during execution. Concurrent source edits are outside this guarantee. A different worktree or clone must match the checkpoint HEAD. Verification cannot be applied to finished tasks. Save the name again to reopen.

Pattern-based redaction reduces accidental secret storage. It cannot recognize every secret. Checkpoint state and captured context are private, untrusted data. Review before sharing. Never put state directories in public Git. State paths are trusted user choices, not a sandbox against another process running as your OS account. Use local storage with reliable POSIX file locks. Network filesystems and Windows are unsupported. Python interfaces are internal in v0.1.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m pip wheel --no-deps . -w dist
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
