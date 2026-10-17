# Agent Checkpoint

Coding work doesn't always fit in one agent session. You can be halfway through a change when the conversation runs out of room, your terminal closes, or you need to switch tasks. The code is still there, but the next session needs to know why you made a decision, what you already checked, and what to do next.

We built Agent Checkpoint so an agent can leave a useful handoff before it stops. Save the goal, decisions, blockers, and next action under a task name, then load that checkpoint when you're ready to continue. When the work is done, run a verification command through the tool so the checkpoint records what actually passed.

The work log also makes the agent's work observable. You can look back at recorded goals, decisions, blockers, and test results to see how the work progressed across sessions and what the agent finished. That gives you something concrete to review when you're running several tasks or handing work between agents.

It started in my dotfiles, and we pulled it into its own tool so you can use it without adopting the rest of my setup.

Python 3.11+, macOS or Linux, and Git for repository awareness. No runtime dependencies. Checkpoints stay on your machine.

## Install

From a checkout:

```sh
python3 -m pip install .
```

For an offline install from a checkout, with no pip or build dependencies:

```sh
python3 scripts/install.py
```

The installer verifies an immutable runtime before switching the active version. It does not move checkpoint state or install agent hooks.

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

## Session history and observability

Resuming a task is only part of the problem. We also want to review what the agent worked toward, where it got stuck, and what evidence it left behind. The work log brings those records together, so you can follow a task from its goal through a failed check, a resolved blocker, and a verified outcome.

Here's a synthetic parser task carried across three sessions. The project already has a parser and tests; the first check fails, and the agent fixes the parser before the final check.

```sh
# Session 1: set the goal and leave the next step.
agent-checkpoint save --name parser \
  --goal "Parse comma-separated values" \
  --next "Handle empty input" \
  --decision "Keep empty input as an empty list" \
  --artifact parser.py

# Session 2: record a failed check and the blocker it exposed.
agent-checkpoint verify parser --run "python3 -m unittest -q"
agent-checkpoint save --name parser \
  --next "Fix empty-input handling" \
  --blocker "Empty input returns one blank field"

# Session 3: after fixing the parser, record the decision and check again.
agent-checkpoint save --name parser \
  --next "Run the tests again" --clear-blocker \
  --decision "Strip surrounding whitespace before splitting"
agent-checkpoint complete parser \
  --run "python3 -m unittest -q" \
  --note "Empty input and whitespace cases pass"

agent-checkpoint log
```

Output from that example, with dates and commit IDs that will vary:

```text
# Work log

## 2026-10-16 parser (complete)

- Repo: parser-demo on main at 339489cd, started 2026-10-16
- Goal: Parse comma-separated values
- Outcome: Empty input and whitespace cases pass
- Artifacts: `parser.py`
- Decisions:
  - Keep empty input as an empty list
  - Strip surrounding whitespace before splitting
- Blockers hit: Empty input returns one blank field
- Verification: python3 -m unittest -q [failed]; python3 -m unittest -q [passed]
```

To review work across projects, use the same records as Markdown or JSON:

```sh
# Current goals, blockers, and next steps for unfinished work.
agent-checkpoint list --status open --json

# Finished work across repositories over the last 30 days.
agent-checkpoint log --all-repos --since 30

# Structured outcomes and retained evidence for your own reports.
agent-checkpoint log --all-repos --since 30 --json
```

By default, the log includes completed tasks and work marked done elsewhere. You can also include abandoned or superseded tasks with `--include`. When one checkpoint supersedes another, the log folds the older decisions and blockers into the finished task so you can review the work together.

Every successful update now records an immutable event, including the previous goal and next action. The named checkpoint is the current view of that history, while `history` lets you review the earlier revisions. The work log stays compact and shows the latest five checks per task; the history retains earlier recorded checks as well. Transcript capture records task context, rather than every message in a conversation.

Record explicit identities when you know which session and agent are doing the work:

```sh
agent-checkpoint --agent-id builder --session-id session-1 \
  save --name parser --goal "Build the parser" --next "Handle empty input"
agent-checkpoint --agent-id reviewer --session-id session-2 resume parser --context
agent-checkpoint history parser
agent-checkpoint timeline --all-repos --since 7 --limit 100 --json
agent-checkpoint stats --all-repos --since 30
agent-checkpoint handoff parser --to-agent reviewer --to-session session-2
```

You can also set `AGENT_CHECKPOINT_AGENT_ID` and `AGENT_CHECKPOINT_SESSION_ID`. Missing identities stay unknown, and reports show that gap instead of guessing who did the work. Use `link TASK --parent PARENT` to connect a smaller task to its parent goal.

History and timeline pages return `next_cursor` and `has_more`. Pass the cursor back with `--after` to continue in commit order, even when several events share a timestamp. Statistics report recorded goal changes, handoffs, reopens, verification outcomes, blocked wall time, and command duration. They do not estimate active effort, model quality, tokens, or cost. Imported tasks are labeled as baselines because earlier revisions cannot be recovered.

## Commands

`save`, `list`, `offer`, `resume`, `verify`, `complete`, `close`, `log`, `dedupe`, `capture`, `history`, `timeline`, `stats`, `handoff`, `link`, and `export` have `--help`. Most commands output JSON. `list` defaults to a table, `log` to Markdown, and `resume --context` to readable text. Use `list --json` and `log --json` for scripts. Verification command output goes to stderr, keeping stdout parseable. Exit status: 0 success, 1 operational failure, 2 argument error. `offer` with no matching task returns 1 and `null`.

## OpenTelemetry export

The local history is useful on its own, and you can export it when you want to review agent work alongside the rest of your telemetry. The exporter uses Python's standard library to produce OTLP/HTTP JSON, with no SDK or collector required on the agent machine.

```sh
# Produce an OTLP log payload without making a network request.
agent-checkpoint export --format otlp --signal logs --all-repos > agent-events.otlp.json

# Send recorded verification spans to an explicitly selected local collector.
agent-checkpoint export --format otlp --signal traces \
  --endpoint http://127.0.0.1:4318/v1/traces
```

Logs carry recorded task events, and verification spans connect command starts with their results and measured duration. An attempt with no recorded result appears as incomplete coverage, rather than a fabricated completed span. Export uses stable identifiers, so you can correlate repeated exports and handle duplicates in your collector.

Default exports hash task and actor identities and omit goals, decisions, paths, commands, and artifacts. `--include-content` includes recorded text after best-effort redaction, so review that potentially sensitive content before sending it. An explicit `--endpoint` is required for a network request; ordinary checkpoint commands never send telemetry. HTTPS is required beyond loopback unless you explicitly pass `--allow-insecure`. Optional authentication headers come from `OTEL_EXPORTER_OTLP_HEADERS`.

Export pages use `--limit` and `--after`, and payloads are capped at 8 MiB. Without an endpoint, stdout contains the OTLP payload and stderr contains cursor and coverage metadata. With an endpoint, stdout reports delivery and coverage. Rejected, malformed, or partial collector responses fail the export without changing local history, and partial responses are not retried automatically.

## State and migration

State selection, highest priority first:

1. Global `--state-dir PATH` before the subcommand.
2. `AGENT_CHECKPOINT_STATE`.
3. Legacy `AGENT_HARNESS_STATE`.
4. `$XDG_STATE_HOME/agent-checkpoint`, or `~/.local/state/agent-checkpoint`.

Existing schema-1 JSON records remain readable, and the first history mutation imports them as labeled baseline snapshots. Back up the state directory and stop legacy checkpoint writers and hooks before switching implementations. Restart agent sessions that loaded old hooks, because those writers cannot participate in the new history protocol. Installation does not copy records or change hooks. After migration, recovery means reinstalling a compatible standalone version; returning to a legacy snapshot writer can lose newly recorded work.

A project can use `.agent/harness.toml` to set the resume context budget with `checkpoint.resume_context_tokens` and the maximum age of offered tasks with `checkpoint.max_offer_age_days`. The defaults are 2000 tokens and 30 days, and the tool ignores other harness settings. To find tasks across checkouts, it matches the repository path, the shared Git directory for worktrees, or the normalized origin URL for clones.

## Agent instructions

Add to your agent instructions:

> Save a named checkpoint after material decisions or before interruptions. Include the goal, next action, decisions, blockers, and artifact paths. Resume explicitly with `agent-checkpoint resume NAME --context`. Complete using a verification command run through `agent-checkpoint complete NAME --run 'COMMAND'`. Use a manual override only when explicitly authorized.

Optional transcript capture:

```sh
agent-checkpoint capture --transcript /path/to/session.jsonl --event compact --cwd /path/to/project
```

Use repeated `--exclude-prefix TEXT` options to omit application-specific prompts. Adapters recognize Claude and Codex JSONL event shapes. They use explicit provenance metadata, not personal prompt matchers. Unsupported formats may produce `null`. Capture is bounded to 32 MiB total and 1 MiB per event. The supported Python integration API also records transcript pointers and recovers them after an unclean exit; downstream hooks can delegate that recovery to this package. Hook installation is opt-in: call the command above from your agent's lifecycle hook with its transcript and cwd. The tool leaves your settings alone, and the synthetic tests show which event shapes the adapters support. Check those examples when a transcript format changes.

## Guarantees and limits

Local history files use mode 0600 and state directories mode 0700. Cooperative advisory locking serializes reads and mutations. Each command commits its task changes and observations together in one immutable JSON batch, then refreshes the current checkpoint files as a disposable cache. If a cache write fails, the committed history still reconstructs the task. Multi-task close, supersede, and dedupe changes share that commit boundary.

Sequence numbers and a hash chain detect gaps and alterations before a surviving later event. They do not authenticate history against your OS account or prove that its tail was never removed, so keep consistent backups. History is not pruned automatically. A post-rename durability error includes its sequence and batch ID; inspect `history --json` before retrying, and remember that seeing a file does not prove it would survive power loss. Reads reject corrupt committed history rather than silently repairing it.

Verification runs without holding the lock. The tool records a start before running the command, then records its result or conflict under the same attempt ID. Completion rejects a changed task revision, repository, or HEAD. It records the command result for the observed HEAD. It does not guarantee that uncommitted working files stayed unchanged during execution. Concurrent source edits are outside this guarantee. A different worktree or clone must match the checkpoint HEAD. To verify a finished task again, save the same name to reopen it first.

The tool redacts recognizable secret patterns, but it cannot catch every credential, so review checkpoint files before sharing them and keep state directories out of public Git. Treat captured context as untrusted text. You choose the state directory, and its file permissions cannot isolate it from another process running as your OS account. Use local storage with reliable POSIX file locks; network filesystems and Windows are unsupported. The command line and `agent_checkpoint.integration` are the supported interfaces in v0.2; other Python modules are internal. See [the integration contract](docs/integration.md).

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m pip wheel --no-deps . -w dist
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
