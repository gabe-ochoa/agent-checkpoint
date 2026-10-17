# Requirements

1. Retain every committed task revision and recorded session/agent identity in immutable JSON event files. No SQLite or third-party runtime dependency.
2. Commit authoritative history atomically. Current checkpoint JSON is a disposable projection; failed cache writes must not lose acknowledged events. Multi-record operations commit as one event batch.
3. Import valid legacy records as explicitly labeled baseline snapshots on first mutation. Reject corrupt legacy input before committing. Ignore stale caches once history exists. Never invent earlier events.
4. Provide history, cross-repository timeline, outcome stats, actor metadata and explicit parent/handoff links. Show coverage gaps and wall-clock durations without implying active effort or model quality.
5. Verification start/result/conflict events share an attempt ID. Commands run outside the lock. Failed or interrupted commands must remain observable and never complete a changed record.
6. Add optional OTLP/HTTP JSON export through stdlib; normal operations never access the network. Default export omits task text, paths and commands. Handle failed and partial responses without mutating task state.
7. Keep the old CLI and schema-1 reads. Add lifecycle journal recovery to the standalone package, then replace downstream implementations with delegation and documented ownership.
8. Install a content-addressed package runtime without pip or build dependencies. Downstream pins an upstream commit, preserves existing state selection, and points future maintenance to the upstream repository.
9. Test reference-model replay, random transitions, multiprocess writers, killed writers, corrupt/missing/reordered events, cache failure, baseline migration, exporter wire format and installed CLI plus hook integration.
