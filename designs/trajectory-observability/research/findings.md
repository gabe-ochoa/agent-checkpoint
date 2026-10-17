# Findings

Current code replaces each task JSON on save and bounds decisions, verification, and blocker history to 20 entries (`src/agent_checkpoint/core.py`, save_checkpoint). Session identity is one current field, so older sessions cannot be reconstructed. Verification runs outside the state lock and compares the record revision and Git HEAD afterward (`service.py`, run_verification). Existing storage already uses atomic replacement, fsync, private permissions and a state lock (`storage.py`).

Downstream integration has a second checkpoint implementation, lifecycle journals, and a CLI wrapper inside its harness. Those must delegate to the standalone package to avoid incompatible writers. Transcript recovery needs to move into the package as well.

OTLP supports JSON over HTTP POST, lowerCamelCase field names, integer enums, hexadecimal trace/span IDs and decimal-string 64-bit timestamps. Partial-success responses must not be automatically retried. Source: https://opentelemetry.io/docs/specs/otlp/ (consulted 2026-10-17).

Constraints approved by the user: no SQLite, no third-party runtime dependencies, immutable file-based history, optional OTel. The user explicitly authorized planning, building, local installation and downstream migration. No claim is made about reconstructing pre-migration history or model token usage.
