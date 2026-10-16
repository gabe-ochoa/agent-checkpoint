# Contributing

Use Python 3.11 or newer on macOS or Linux. Install with `python3 -m pip install -e .`. Run `make test` before proposing a change. Run `make test-failure` to exercise persistence failures, concurrent updates, and verification conflicts.

For a release candidate, build a wheel with `python3 -m pip wheel --no-deps . -w dist`, then run `python3 scripts/validate_wheel.py dist/agent_checkpoint-0.1.0-py3-none-any.whl`. This installs into a fresh environment, runs the tests outside the checkout, and exercises every command.

Storage changes need an independent oracle and failure tests before implementation. Preserve schema-1 reading. Avoid silent repair or data migration. Include synthetic fixtures only. Do not submit real transcripts, checkpoint files, secrets, or private repository paths.

Python interfaces are internal in v0.1. The CLI and file compatibility contract are the supported interfaces. Changes to them need migration notes and tests. No network is required at runtime.
