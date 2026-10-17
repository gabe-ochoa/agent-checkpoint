"""Private JSON records and cooperative per-state locking."""

import errno
import fcntl
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .redaction import redact_secrets


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,63}", value
    ):
        raise ValueError(
            "invalid checkpoint ID; use lowercase letters, digits and hyphens (max 64)"
        )
    return value


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def ensure_private_dir(path):
    if path.is_symlink():
        raise ValueError(f"symlinked state directory: {path}; select a real directory")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for created in reversed(missing):
        created.chmod(0o700)
        fsync_directory(created)
        fsync_directory(created.parent)
    if path.stat().st_mode & 0o777 != 0o700:
        path.chmod(0o700)
        fsync_directory(path)


def reject_link(path):
    if path.is_symlink():
        raise ValueError(
            f"symlinked state file: {path}; inspect and remove the link manually"
        )


@contextmanager
def locked(state, timeout=10):
    state = Path(state).expanduser().absolute()
    ensure_private_dir(state)
    path = state / ".lock"
    reject_link(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(
                f"state lock is not a regular file: {path}; inspect it manually"
            )
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"lock timeout: {path}; wait for the other checkpoint process and retry"
                    )
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


def atomic_write(path, data, mode=0o600):
    ensure_private_dir(path.parent)
    reject_link(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def validate_record(record, path):
    def fail():
        raise ValueError(
            f"corrupt or unsupported checkpoint: {path}; inspect or restore a backup, no automatic repair"
        )

    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
    ):
        fail()
    try:
        validate_id(record.get("id"))
    except ValueError:
        fail()
    if record["id"] != path.stem or record.get("status") not in (
        "open",
        "complete",
        "closed",
    ):
        fail()
    for key in ("repo", "goal", "next_action", "created_at", "updated_at"):
        if not isinstance(record.get(key), str):
            fail()
    for key in ("created_at", "updated_at", "closed_at"):
        if key not in record:
            continue
        try:
            parsed = datetime.fromisoformat(record[key].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                fail()
        except (ValueError, TypeError, AttributeError):
            fail()
    for key in (
        "name",
        "branch",
        "head",
        "remote",
        "git_common_dir",
        "blocker",
        "blocker_since",
        "outcome",
        "close_reason",
        "superseded_by",
        "session_id",
        "platform",
        "last_material_change_at",
        "parent_task_id",
    ):
        if key in record and not isinstance(record[key], str):
            fail()
    for key in ("decisions", "artifacts", "changed_files", "supersedes"):
        if key in record and (
            not isinstance(record[key], list)
            or not all(isinstance(v, str) for v in record[key])
        ):
            fail()
    if record.get("parent_task_id"):
        try:
            validate_id(record["parent_task_id"])
        except ValueError:
            fail()
    for value in record.get("supersedes", []):
        try:
            validate_id(value)
        except ValueError:
            fail()
    if record.get("superseded_by"):
        try:
            validate_id(record["superseded_by"])
        except ValueError:
            fail()
    for key in ("verification", "blocker_history"):
        if not isinstance(record.get(key, []), list):
            fail()
    if record.get("status") == "closed" and record.get("close_reason") not in (
        "abandoned",
        "superseded",
        "done-elsewhere",
    ):
        fail()
    for entry in record.get("verification", []):
        if isinstance(entry, str):
            continue  # legacy pending note
        if not isinstance(entry, dict):
            fail()
        for key in ("command", "status", "completed_at", "head"):
            if key in entry and not isinstance(entry[key], str):
                fail()
        if "command" not in entry:
            fail()
        if entry.get("status", "pending") not in (
            "pending",
            "passed",
            "failed",
            "manual",
            "unresolved",
        ):
            fail()
        if entry.get("exit_code") is not None and type(entry["exit_code"]) is not int:
            fail()
    for entry in record.get("blocker_history", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("blocker"), str):
            fail()
        if any(not isinstance(v, str) for v in entry.values()):
            fail()
    return record


def sanitized(value):
    if isinstance(value, str):
        return redact_secrets(value)[0]
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitized(v) for k, v in value.items() if not k.startswith("_")}
    return value


def read_record_file(path):
    reject_link(path.parent)
    reject_link(path)
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError(f"checkpoint exceeds 8 MiB: {path}; inspect it manually")
        with path.open(encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError(
            f"corrupt checkpoint: {path}; restore a backup or repair manually"
        ) from exc
    validate_record(record, path)
    record["_path"] = str(path)
    return record


def write_record_file(path, record):
    record = sanitized(record)
    validate_record(record, path)
    atomic_write(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return dict(record, _path=str(path))


def active_for(state):
    from .journal import ACTIVE

    tx = ACTIVE.get()
    return (
        tx
        if tx is not None and tx.state == Path(state).expanduser().absolute()
        else None
    )


def read_record(path):
    path = Path(path).absolute()
    tx = active_for(path.parent.parent)
    if tx is not None and path.parent == tx.state / "checkpoints":
        reject_link(path.parent)
        reject_link(path)
        record = tx.records.get(path.stem)
        return dict(deepcopy(record), _path=str(path)) if record is not None else None
    return read_record_file(path)


def write_record(path, record):
    path = Path(path).absolute()
    tx = active_for(path.parent.parent)
    if tx is not None:
        reject_link(path.parent)
        reject_link(path)
        return tx.write(path, record)
    if (path.parent.parent / "events").exists():
        raise ValueError(
            "checkpoint history is active; mutate through the standalone service"
        )
    return write_record_file(path, record)
