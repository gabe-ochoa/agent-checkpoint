# Integration and maintenance contract

The command line and `agent_checkpoint.integration` are supported within 0.2.x. Internal modules may change. The integration module exposes checkpoint CRUD, verification, history/timeline/stats, actor contexts, parent/handoff links, and lifecycle journal recovery. Functions accept a `Path` state directory explicitly and return schema-1 task dictionaries, report dictionaries, or `None` when no capture applies. Record dictionaries may include an internal `_path`; do not treat it as portable data. Input/schema failures raise `ValueError`, filesystem failures raise `OSError`, and conflicts or uncertain commits raise `RuntimeError`.

```python
from pathlib import Path
from agent_checkpoint import integration as checkpoint

state = Path.home() / ".local/state/agent-checkpoint"
with checkpoint.actor_context(agent_id="builder", session_id="session-1"):
    checkpoint.save_checkpoint(
        state, Path.cwd(), "Build the parser", "Run checks", name="parser"
    )
page = checkpoint.history(state, "parser", limit=100)
checkpoint.record_journal(
    state,
    {"session_id": "session-1", "transcript_path": "/path/to/session.jsonl"},
    Path.cwd(),
)
checkpoint.consolidate_journals(state, Path.cwd())
```

State selection belongs to the caller; CLI precedence is documented in the README. Downstream harnesses should preserve their existing explicit state directory when migrating. Never run legacy snapshot writers against history-enabled state. Restart agents that loaded legacy hooks before migration. Already-loaded old code cannot be fenced by this file format.

`python3 scripts/install.py` installs without pip or build dependencies. It copies only Python package sources into a content-addressed runtime. `MANIFEST.json` contains schema version, source Git revision, and a map of relative package file paths to SHA-256 hashes. The content ID is the SHA-256 of the canonical JSON manifest excluding `content_id`. The launcher validates paths, the complete file set, hashes and selected runtime identity before importing code. The current pointer switches atomically after validation. Runtime roots resolve once per invocation. Installation never mutates task state.

A downstream repository should own only its source commit pin, local policy and a thin adapter. Make storage, reporting, capture and recovery changes in this upstream repository. Test and commit upstream, advance the downstream pin to that exact commit, install it, then verify both CLI and native hooks. Preserve a private state backup before the first history mutation. An unmerged reviewed branch can be pinned for local validation. Merge and deploy decisions remain with the downstream owner.

Events retain earlier recorded values indefinitely unless the user explicitly manages history. Current checkpoint projections keep bounded resume fields. Baseline imports preserve available legacy data and source fingerprints, not invented earlier revisions. Missing actor IDs and unfinished verification attempts are explicit coverage gaps. Export is manual, bounded and independent from task mutation; repeat delivery can duplicate telemetry.
