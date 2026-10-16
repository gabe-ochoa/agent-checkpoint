# Security

Checkpoint files contain private work context. Keep them outside public repositories. Review anything you share. Pattern redaction is incomplete by design.

Only caller-supplied `verify --run` and `complete --run` commands execute a shell. Captured transcripts are untrusted context. The tool does not execute captured text or infer verified completion from assistant prose.

State paths are trusted user choices. Permissions and symlink rejection reduce accidental exposure; they do not isolate processes running as your OS account. Use local storage with reliable POSIX advisory locking. Do not use legacy writers against the same state concurrently.

For sensitive reports, use GitHub's private vulnerability reporting if enabled. Otherwise open an issue requesting a private contact channel without including exploit details, private data, or credentials. Do not assume private reporting is enabled before checking the repository UI.
