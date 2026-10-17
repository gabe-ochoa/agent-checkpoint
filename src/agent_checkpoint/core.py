from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .git import git_snapshot, repo_identity, same_repo
from .redaction import redact_secrets
from .storage import (
    ensure_private_dir,
    read_record,
    validate_id,
    write_record,
)

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _checkpoint_dir(state_dir: Path) -> Path:
    return state_dir / "checkpoints"


def _checkpoint_path(state_dir: Path, checkpoint_id: str) -> Path:
    return _checkpoint_dir(state_dir) / f"{validate_id(checkpoint_id)}.json"


NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

CLOSE_REASONS = ("superseded", "abandoned", "done-elsewhere")

STALE_CHECK_HOURS = 24

BUILTIN_COMMANDS = {
    "/add-dir",
    "/agents",
    "/clear",
    "/color",
    "/compact",
    "/config",
    "/context",
    "/cost",
    "/doctor",
    "/effort",
    "/exit",
    "/export",
    "/fast",
    "/help",
    "/hooks",
    "/ide",
    "/init",
    "/login",
    "/logout",
    "/mcp",
    "/memory",
    "/model",
    "/permissions",
    "/plugin",
    "/resume",
    "/rewind",
    "/status",
    "/statusline",
    "/theme",
    "/usage",
    "/vim",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def slugify(text: str, limit: int = 40) -> str:
    """Turn free text into a short readable checkpoint name."""
    text = ANSI_RE.sub(" ", re.sub(r"<[^>]+>", " ", text or "")).lower()
    words = re.findall(r"[a-z0-9]+", text)
    slug = ""
    for word in words:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > limit:
            break
        slug = candidate
    return slug or (words[0][:limit] if words else "task")


def validate_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ValueError(
            f"invalid checkpoint name {name!r}: use lowercase letters, digits, and hyphens (max 64)"
        )
    return name


def clean_prompt(text: str) -> str:
    """Render a transcript prompt as plain task text; empty means no task content."""
    text = ANSI_RE.sub("", text or "")
    if "<local-command-stdout>" in text or "<local-command-stderr>" in text:
        return ""
    command = re.search(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>", text)
    if command:
        name = (
            command.group(1)
            if command.group(1).startswith("/")
            else "/" + command.group(1)
        )
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.DOTALL)
        args_text = " ".join((args.group(1) if args else "").split())
        if name in BUILTIN_COMMANDS:
            return ""
        return f"{name} {args_text}".strip()
    text = re.sub(r"\[(@[^\]]+)\]\([a-z]+://[^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def first_sentence(text: str, limit: int = 240) -> str:
    text = " ".join(re.sub(r"[*_`#>]+", "", text or "").split())
    match = re.match(r"(.+?[.!?])(?:\s|$)", text)
    sentence = match.group(1) if match else text
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0] + "..."
    return sentence


def _checkpoint_id(repo: str, session_id: str | None = None, goal: str = "") -> str:
    if session_id:
        return hashlib.sha256(f"{repo}\0{session_id}".encode()).hexdigest()[:16]
    return f"{slugify(goal)}-{uuid.uuid4().hex[:4]}"


def _read_checkpoint(path):
    record = read_record(path)
    if record is not None:
        record.setdefault("name", slugify(record["goal"]))
    return record


def _write_checkpoint(path, record):
    return write_record(path, record)


def resolve_checkpoint(state_dir: Path, ref: str) -> dict[str, Any]:
    """Find a checkpoint by exact id, then name, then unique id prefix."""
    validate_id(ref)
    record = _read_checkpoint(_checkpoint_path(state_dir, ref))
    if record:
        return record
    records = list_checkpoints(state_dir)
    for matcher in (
        lambda item: item.get("name") == ref,
        lambda item: str(item.get("id", "")).startswith(ref),
    ):
        matches = [item for item in records if matcher(item)]
        open_matches = [item for item in matches if item.get("status") == "open"]
        candidates = open_matches or matches
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            ids = ", ".join(str(item["id"]) for item in candidates[:5])
            raise ValueError(f"checkpoint {ref!r} is ambiguous: {ids}")
    raise FileNotFoundError(f"checkpoint not found: {ref}")


def stale_checks(record: dict[str, Any], hours: int = STALE_CHECK_HOURS) -> int:
    """Count pending verification entries older than the given age."""
    if record.get("status") != "open":
        return 0
    pending = [
        item
        for item in record.get("verification", [])
        if isinstance(item, dict) and item.get("status") == "pending"
    ]
    if not pending:
        return 0
    try:
        since = datetime.fromisoformat(
            str(record.get("created_at", "")).replace("Z", "+00:00")
        )
    except ValueError:
        return len(pending)
    return len(pending) if datetime.now(UTC) - since > timedelta(hours=hours) else 0


def save_checkpoint(
    state_dir: Path,
    cwd: Path,
    goal: str,
    next_action: str,
    decisions: list[str] | None = None,
    verification: list[Any] | None = None,
    blocker: str | None = None,
    session_id: str | None = None,
    platform: str = "manual",
    checkpoint_id: str | None = None,
    name: str | None = None,
    supersedes: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update a checkpoint. blocker=None keeps the current blocker; "" clears it."""
    generated = checkpoint_id is None and name is None and session_id is None
    snapshot = git_snapshot(cwd)
    identity = repo_identity(cwd)
    if name:
        checkpoint_id = checkpoint_id or validate_name(name)
    checkpoint_id = checkpoint_id or _checkpoint_id(snapshot["repo"], session_id, goal)
    path = _checkpoint_path(state_dir, checkpoint_id)
    if generated:
        for _ in range(100):
            if not path.exists() and not path.is_symlink():
                break
            checkpoint_id = _checkpoint_id(snapshot["repo"], None, goal)
            path = _checkpoint_path(state_dir, checkpoint_id)
        else:
            raise RuntimeError("unable to allocate a unique checkpoint ID; use --name")
    ensure_private_dir(state_dir)
    ensure_private_dir(path.parent)
    existing = _read_checkpoint(path) or {}
    if name and existing and not same_repo(existing, identity):
        raise ValueError(
            f"checkpoint name {name!r} already belongs to {existing.get('repo')}; pick another name"
        )
    goal = goal or existing.get("goal", "")
    if not goal:
        raise ValueError("a new checkpoint requires --goal")
    superseded = [resolve_checkpoint(state_dir, ref) for ref in supersedes or []]
    if any(item["status"] != "open" for item in superseded):
        raise ValueError("supersede targets must be open; inspect them before retrying")
    if any(item["id"] == checkpoint_id for item in superseded):
        raise ValueError("a checkpoint cannot supersede itself")

    def clean(value: str, limit: int) -> str:
        return redact_secrets(value or "")[0][:limit]

    merged_decisions = list(existing.get("decisions", []))
    for decision in decisions or []:
        sanitized = clean(decision, 500)
        if sanitized and sanitized not in merged_decisions:
            merged_decisions.append(sanitized)
    merged_verification = list(existing.get("verification", []))
    for result in verification or []:
        if isinstance(result, dict):
            sanitized = {
                "command": clean(str(result.get("command", "")), 500),
                "exit_code": result.get("exit_code"),
                "status": clean(str(result.get("status", "pending")), 40),
                "completed_at": clean(str(result.get("completed_at", "")), 80),
                "head": clean(str(result.get("head", "")), 80),
            }
        else:
            sanitized = {
                "command": clean(str(result), 500),
                "exit_code": None,
                "status": "pending",
                "completed_at": "",
                "head": "",
            }
        if sanitized["command"] and sanitized not in merged_verification:
            merged_verification.append(sanitized)

    current_blocker = existing.get("blocker", "")
    new_blocker = current_blocker if blocker is None else clean(blocker, 1000)
    blocker_history = list(existing.get("blocker_history", []))
    blocker_since = (
        existing.get("blocker_since", "")
        if new_blocker == current_blocker
        else now_iso()
    )
    if current_blocker and new_blocker != current_blocker:
        blocker_history.append(
            {
                "blocker": current_blocker,
                "since": existing.get("blocker_since", ""),
                "until": now_iso(),
            }
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "id": checkpoint_id,
        "name": name or existing.get("name") or slugify(goal),
        "status": "open",
        "created_at": existing.get("created_at", now_iso()),
        "updated_at": now_iso(),
        "last_material_change_at": now_iso()
        if snapshot["dirty"]
        else existing.get("last_material_change_at", ""),
        "platform": platform,
        "session_id": session_id or existing.get("session_id", ""),
        "repo": snapshot["repo"],
        "git_common_dir": identity["git_common_dir"],
        "remote": identity["remote"],
        "branch": snapshot["branch"],
        "head": snapshot["head"],
        "dirty": snapshot["dirty"],
        "changed_files": snapshot["changed_files"][:200],
        "goal": clean(goal, 1000),
        "decisions": merged_decisions[-20:],
        "verification": merged_verification[-20:],
        "blocker": new_blocker,
        "blocker_since": blocker_since if new_blocker else "",
        "blocker_history": blocker_history[-20:],
        "next_action": clean(next_action, 1000),
    }
    _annotate(payload, "", list(existing.get("artifacts", [])) + list(artifacts or []))
    if existing.get("supersedes") or superseded:
        payload["supersedes"] = sorted(
            set(existing.get("supersedes", [])) | {item["id"] for item in superseded}
        )
    record = _write_checkpoint(path, payload)
    try:
        for item in superseded:
            close_checkpoint(
                state_dir, item["id"], "superseded", superseded_by=checkpoint_id
            )
    except OSError as exc:
        raise RuntimeError(
            "supersede failed before history commit; transaction discarded"
        ) from exc
    return record


def list_checkpoints(
    state_dir: Path, status: str | None = None
) -> list[dict[str, Any]]:
    from copy import deepcopy

    from .storage import active_for

    tx = active_for(state_dir)
    if tx is not None:
        directory = _checkpoint_dir(state_dir)
        if directory.is_symlink():
            raise ValueError("symlinked checkpoints directory")
        records = [
            dict(deepcopy(record), _path=str(directory / f"{identifier}.json"))
            for identifier, record in tx.records.items()
            if status is None or record["status"] == status
        ]
        return sorted(records, key=lambda item: item["updated_at"], reverse=True)
    records: list[dict[str, Any]] = []
    directory = _checkpoint_dir(state_dir)
    if directory.is_symlink():
        raise ValueError("symlinked checkpoints directory; select a real directory")
    for path in (
        _checkpoint_dir(state_dir).glob("*.json")
        if _checkpoint_dir(state_dir).exists()
        else []
    ):
        record = _read_checkpoint(path)
        if record and (status is None or record.get("status") == status):
            records.append(record)
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def offer_checkpoints(
    state_dir: Path, cwd: Path, max_age_days: int = 30, limit: int = 3
) -> list[dict[str, Any]]:
    """Return the newest open checkpoints for this repository, its worktrees, and clones."""
    current = git_snapshot(cwd)
    identity = repo_identity(cwd)
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    offers: list[dict[str, Any]] = []
    for record in list_checkpoints(state_dir, "open"):
        try:
            updated = datetime.fromisoformat(
                str(record.get("updated_at", "")).replace("Z", "+00:00")
            )
            if updated < cutoff:
                continue
        except ValueError:
            continue
        if not same_repo(record, identity):
            continue
        record["diverged"] = bool(
            record.get("repo") != current["repo"]
            or (
                record.get("branch")
                and current["branch"]
                and record.get("branch") != current["branch"]
            )
            or (
                record.get("head")
                and current["head"]
                and record.get("head") != current["head"]
            )
        )
        offers.append(record)
        if len(offers) >= limit:
            break
    return offers


def offer_checkpoint(
    state_dir: Path, cwd: Path, max_age_days: int = 30
) -> dict[str, Any] | None:
    offers = offer_checkpoints(state_dir, cwd, max_age_days, limit=1)
    return offers[0] if offers else None


def _verification_entry(
    command: str, exit_code: int | None, status: str, head: str
) -> dict[str, Any]:
    return {
        "command": redact_secrets(command)[0][:500],
        "exit_code": exit_code,
        "status": status,
        "completed_at": now_iso(),
        "head": head,
    }


def _record_verification(record: dict[str, Any], entry: dict[str, Any]) -> None:
    """Replace a pending entry for the same command, else append."""
    checks = record.setdefault("verification", [])
    for index, item in enumerate(checks):
        if (
            isinstance(item, dict)
            and item.get("status") == "pending"
            and item.get("command") == entry["command"]
        ):
            checks[index] = entry
            return
    checks.append(entry)
    record["verification"] = checks[-20:]


def verify_checkpoint(
    state_dir: Path, checkpoint_ref: str, command: str, exit_code: int
) -> dict[str, Any]:
    """Record a verification result without changing checkpoint status."""
    record = resolve_checkpoint(state_dir, checkpoint_ref)
    require_open(record)
    head = git_snapshot(Path(record["repo"]))["head"]
    status = "passed" if exit_code == 0 else "failed"
    _record_verification(record, _verification_entry(command, exit_code, status, head))
    record["updated_at"] = now_iso()
    return _write_checkpoint(Path(record["_path"]), record)


def _annotate(
    record: dict[str, Any], note: str = "", artifacts: list[str] | None = None
) -> None:
    """Record what came of the work and where it lives, for the work log."""
    if note:
        record["outcome"] = redact_secrets(note)[0][:1000]
    merged = list(record.get("artifacts", []))
    for artifact in artifacts or []:
        cleaned = redact_secrets(artifact)[0][:300]
        if cleaned and cleaned not in merged:
            merged.append(cleaned)
    if merged:
        record["artifacts"] = merged[-30:]


def complete_checkpoint(
    state_dir: Path,
    checkpoint_id: str,
    verification_command: str = "",
    exit_code: int | None = None,
    manual: bool = False,
    note: str = "",
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    record = resolve_checkpoint(state_dir, checkpoint_id)
    require_open(record)
    path = Path(record["_path"])
    snapshot = git_snapshot(Path(record["repo"]))
    if not manual:
        if not verification_command or exit_code != 0:
            raise ValueError(
                "completion requires a successful verification command or --manual"
            )
        _record_verification(
            record,
            _verification_entry(
                verification_command, exit_code, "passed", snapshot["head"]
            ),
        )
    else:
        record.setdefault("verification", []).append(
            _verification_entry("manual completion", None, "manual", snapshot["head"])
        )
    # Pending notes are moot once the task is done; keep them visible but not pending.
    for item in record.get("verification", []):
        if isinstance(item, dict) and item.get("status") == "pending":
            item["status"] = "unresolved"
    _annotate(record, note, artifacts)
    record["status"] = "complete"
    record["closed_at"] = now_iso()
    record["updated_at"] = now_iso()
    return _write_checkpoint(path, record)


def close_checkpoint(
    state_dir: Path,
    checkpoint_ref: str,
    reason: str,
    superseded_by: str = "",
    note: str = "",
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """Close a checkpoint that will not be completed here."""
    if reason not in CLOSE_REASONS:
        raise ValueError(f"close reason must be one of: {', '.join(CLOSE_REASONS)}")
    record = resolve_checkpoint(state_dir, checkpoint_ref)
    require_open(record)
    record["status"] = "closed"
    record["close_reason"] = reason
    if superseded_by:
        record["superseded_by"] = superseded_by
    _annotate(record, note, artifacts)
    record["closed_at"] = now_iso()
    record["updated_at"] = now_iso()
    return _write_checkpoint(Path(record["_path"]), record)


def dedupe_checkpoints(state_dir: Path, apply: bool = False) -> list[dict[str, str]]:
    """Close older open copies that share a repository and name with a newer one."""
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    actions: list[dict[str, str]] = []
    for record in list_checkpoints(state_dir, "open"):
        key = (
            str(record.get("git_common_dir") or record.get("repo")),
            str(record.get("name")),
        )
        keeper = newest.setdefault(key, record)
        if keeper is record:
            continue
        actions.append({"close": record["id"], "keep": keeper["id"], "name": key[1]})
        if apply:
            try:
                close_checkpoint(
                    state_dir, record["id"], "superseded", superseded_by=keeper["id"]
                )
            except OSError as exc:
                raise RuntimeError(
                    "dedupe failed before history commit; transaction discarded"
                ) from exc
    return actions


FINISHED_REASONS = ("complete", "done-elsewhere", "abandoned", "superseded")


def work_log(
    state_dir: Path,
    cwd: Path | None = None,
    since_days: int | None = 90,
    include: tuple[str, ...] = ("complete", "done-elsewhere"),
) -> list[dict[str, Any]]:
    """Finished checkpoints, newest first, with superseded copies folded into their keeper."""
    records = list_checkpoints(state_dir)
    by_id = {record["id"]: record for record in records}
    reverse = {}
    for item in records:
        if item.get("status") == "closed" and item.get("superseded_by"):
            reverse.setdefault(item["superseded_by"], []).append(item["id"])
    identity = repo_identity(cwd) if cwd else None
    cutoff = datetime.now(UTC) - timedelta(days=since_days) if since_days else None
    entries = []
    for record in records:
        outcome = record.get("close_reason") or record.get("status")
        if record.get("status") == "open" or outcome not in include:
            continue
        if identity and not same_repo(record, identity):
            continue
        finished = str(record.get("closed_at") or record.get("updated_at", ""))
        try:
            if (
                cutoff
                and datetime.fromisoformat(finished.replace("Z", "+00:00")) < cutoff
            ):
                continue
        except ValueError:
            pass
        entry = dict(record, outcome_status=outcome, finished_at=finished)
        # Walk the supersede chain so decisions from earlier copies are not lost.
        pending = list(record.get("supersedes", [])) + reverse.get(record["id"], [])
        seen = {record["id"]}
        decisions = list(record.get("decisions", []))
        blockers = [
            item.get("blocker", "") for item in record.get("blocker_history", [])
        ]
        while pending:
            older = by_id.get(pending.pop())
            if not older or older["id"] in seen or older["status"] == "open":
                continue
            seen.add(older["id"])
            decisions += [d for d in older.get("decisions", []) if d not in decisions]
            blockers += [
                item.get("blocker", "") for item in older.get("blocker_history", [])
            ]
            if older.get("blocker"):
                blockers.append(older["blocker"])
            pending += list(older.get("supersedes", [])) + reverse.get(older["id"], [])
        entry["decisions"] = decisions
        entry["blockers_seen"] = [
            b for i, b in enumerate(blockers) if b and b not in blockers[:i]
        ]
        entry["folded"] = sorted(seen - {record["id"]})
        entries.append(entry)
    return sorted(entries, key=lambda item: item["finished_at"], reverse=True)


def require_open(record):
    if record["status"] != "open":
        raise ValueError(
            f"checkpoint {record['id']} is {record['status']}; save the same name to reopen it"
        )
