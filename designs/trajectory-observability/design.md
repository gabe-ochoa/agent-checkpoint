# Trajectory observability design

## Protocol

A state directory contains `events/<20-digit-sequence>.json` plus disposable schema-1 checkpoint files. Each immutable event batch has a schema version, sequence, previous batch SHA-256, UUID, timestamp, operation, actor (session/agent/platform), full changed task records, and typed observations. One atomic rename and directory fsync commits the whole batch. All reads and mutations share the existing state lock. Event filenames are never reused. A write failure after rename is an ambiguous commit and must say so; callers can inspect history before retrying. Batch IDs permit correlation, not automatic deduplication of arbitrary CLI retries.

Replay validates contiguous sequences, hash chain, required field types, record schemas, task IDs and timestamps. Missing, altered or malformed committed files produce actionable errors. Orphan temporary files are ignored. The chain detects accidental corruption; it is not authentication against the same OS account. No history pruning or repair happens automatically. A future retention feature must preserve explicit gap markers.

A command opens one in-memory transaction under the lock. Existing lifecycle functions read and write that transaction's records. On success, persist one batch, then refresh projection files as a best-effort compatibility cache. On failure before commit, discard all buffered writes. Cache failure after commit warns on stderr but returns the committed result. Replaying history produces the current state regardless of cache contents. This initial implementation favors complete validation over a persisted index; measure its scaling before adding a cache shortcut.

Without history, read existing JSON without changing its bytes. The first successful mutation commits one batch containing all baseline snapshots plus the mutation. Each baseline is labeled imported, with observation time separate from original record timestamps. Historic goals already overwritten by old versions are unavailable. Old writers must be disabled before migration; downstream delegation removes the duplicate implementation.

## Event semantics and views

Task writes record operation and field changes, including old/new goals and next actions, decisions, blockers, status and artifacts. Current records may stay bounded for resume context; history retains the full sequence of revisions. Actor metadata is explicit via CLI or environment and remains unknown when absent. Tasks may link to a parent; handoffs record source/target identities without fabricating a session. Resume records an observation without changing task revision. History selects a stable task ID and returns ordered events. Timeline filters repository, date and actor. JSON is the machine interface; readable text summarizes changes.

Verification records a started event with attempt ID and observed HEAD before launching the shell. The command runs outside the lock. The finish event records duration, exit status and result, or a conflict/abort. Revision conflicts preserve newer task state while recording the attempt as unapplied. A process killed between start and finish leaves an incomplete attempt, shown as unknown/interrupted coverage rather than failed or active time. Reopen and handoff are explicit events. Stats count observed starts, results, verified/manual completions, reopens and blocked wall time. Unknown session IDs and imported baseline coverage are reported. No semantic success score, hidden reasoning capture, token or cost estimate is inferred.

## Export

`export --format otlp --signal logs|traces` produces standard OTLP JSON. Logs cover task events; verification spans use recorded start/end times and deterministic IDs. Task identifiers correlate events and spans. Incomplete attempts are omitted from completed spans and counted in export coverage. Default attributes exclude goals, decisions, paths, commands and artifacts; `--include-content` explicitly includes redacted recorded text. No implicit network export occurs. An explicit endpoint posts a bounded body with application/json and a finite timeout. Partial success is reported without retry; non-200 and malformed responses fail export only. Re-export can duplicate telemetry, so event IDs are stable and collectors may deduplicate. No automatic delivery guarantee is claimed.

## Downstream and installation

Upstream provides stdlib `scripts/install.py`, installing a content-addressed immutable package runtime and launcher. A private current pointer selects a verified manifest. The CLI launcher and downstream Python adapter verify that runtime before importing it. Installation is atomic, idempotent and does not mutate task state. Downstream pins an upstream Git commit, checks origin and checkout cleanliness, and installs only that revision. It can fetch a missing revision but does not overwrite local edits. Existing state stays selected explicitly.

Move lifecycle journal capture/recovery upstream. The downstream harness keeps its policies and unrelated analytics, but removes checkpoint persistence, CLI logic and journal logic. A thin adapter loads the installed upstream package. Future changes belong upstream, followed by updating the downstream pin and reinstalling. Tests validate delegation, generated runtime loading and installed commands. Preserve unrelated downstream edits.

## Validation

Write the reference replay oracle and crash-boundary tests before changing persistence. Prototype commit/replay and measure file-count scaling. Use six expert review lenses, then address material findings before installation. Draft PRs require test-failure, installed-wheel and offline installer validation plus privacy scans. Local installation is authorized; merges and shared collector configuration require separate approval.
