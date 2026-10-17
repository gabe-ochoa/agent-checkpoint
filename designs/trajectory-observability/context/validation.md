# Validation checklist

- No SQLite and no runtime dependencies.
- Replay oracle and fault/concurrency tests pass.
- Current state is rebuildable from committed events.
- Legacy import coverage is explicit; no fabricated history.
- History/timeline/stats/export work from installed package.
- Export wire format and failure handling tested with local receiver.
- Local journals, resume offers and verification behavior preserved through upstream delegation.
- Offline installer and runtime verification pass.
- Required test-failure checks and relevant downstream tests pass.
- User state backed up before live migration; package pin recorded.
- Source/artifact privacy scans pass before branch pushes.
- Draft PRs attached; merges remain unapproved.

## Recorded results

- Python 3.14.7 source suite: 82 tests passed. The added installer failure regression passed separately, 2 installer tests total.
- `make test-failure`: 14 storage, 7 verification and 4 journal fault tests passed.
- Dotfiles delegation/native hook suite: 27 tests passed. Setup pin/origin tests: 2 passed.
- Ruff and diff whitespace checks passed. README voice-lint scored 100/100.
- Source secret scan passed. Confidentiality scan had no blocking findings; its one warning matched a character class in the AWS-key redaction regex.
- Six expert lenses reviewed the design. Security/Operations and Reliability/Data follow-up found no remaining major blockers.
- The private migration backup contains 70 checkpoint files and 21 journal pointers. No private records were copied into this repository.
- Installed 0.2.0 wheel: 83 tests and every CLI command passed outside the checkout. Metadata confirms zero runtime dependencies.
