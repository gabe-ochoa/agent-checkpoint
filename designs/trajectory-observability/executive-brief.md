# Trajectory observability

The present snapshot answers where a task stands, but overwrites how it got there. Immutable events make the tool useful for reviewing agent work as well as resuming it. Keep the local CLI and standard-library installation footprint. Make OpenTelemetry an explicit export adapter.

Build a file-based authoritative event journal and replay model first. Add trajectory views and measured outcomes, then migrate downstream consumers to one tested implementation. Preserve old records as labeled baselines. The user approved the storage constraints and authorized the build; publication uses draft PR review.
