# ADR 001: Immutable JSON batches

Accepted constraints: no SQLite and no third-party runtime dependencies. Use one ordered immutable file per atomic batch, with sequence and previous hash validation. Derive current checkpoint files from history. Rejected independent JSONL-plus-snapshot dual writes because they create a split commit boundary and torn-tail recovery. Full replay is deliberately simple; benchmark scaling before adding persisted indexes.
