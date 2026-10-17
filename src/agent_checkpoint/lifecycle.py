"""Opt-in transcript pointers for recovery after an unclean agent exit."""

import hashlib
import json
from pathlib import Path

from .git import find_repo_root
from .journal import utcnow
from .storage import atomic_write, ensure_private_dir, locked, reject_link


def record_journal(state, event, cwd):
    session = str(event.get("session_id") or event.get("sessionId") or "")
    transcript = event.get("transcript_path")
    if not session or not isinstance(transcript, str) or not transcript:
        return
    state = Path(state).expanduser().absolute()
    with locked(state):
        directory = state / "journals"
        ensure_private_dir(directory)
        identifier = hashlib.sha256(session.encode()).hexdigest()[:16]
        value = {
            "schema_version": 1,
            "session_id": session,
            "transcript_path": transcript,
            "repo": str(find_repo_root(Path(cwd))),
            "updated_at": utcnow(),
        }
        atomic_write(
            directory / f"{identifier}.json", json.dumps(value, sort_keys=True) + "\n"
        )


def consolidate_journals(state, cwd):
    state = Path(state).expanduser().absolute()
    directory = state / "journals"
    pending = []
    with locked(state):
        reject_link(directory)
        if not directory.exists():
            return {"recovered": 0, "failed": 0}
        for path in sorted(directory.glob("*.json")):
            reject_link(path)
            try:
                if path.stat().st_size > 65536:
                    raise ValueError("journal pointer exceeds size limit")
                raw = path.read_bytes()
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get("schema_version") != 1:
                    raise ValueError("unsupported journal pointer")
                if not all(
                    isinstance(value.get(k), str)
                    for k in ("session_id", "transcript_path", "repo")
                ):
                    raise ValueError("invalid journal pointer")
            except (OSError, ValueError):
                continue
            if value["repo"] == str(find_repo_root(Path(cwd))):
                pending.append((path, raw, value))
    recovered = failed = 0
    for path, raw, value in pending:
        try:
            from .transcripts import capture

            capture(state, Path(value["transcript_path"]), Path(cwd), "journal")
            with locked(state):
                reject_link(path)
                if path.exists() and path.read_bytes() == raw:
                    path.unlink()
            recovered += 1
        except (OSError, ValueError, RuntimeError):
            failed += 1
    return {"recovered": recovered, "failed": failed}
