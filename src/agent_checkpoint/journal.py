"""Immutable JSON batch journal. Checkpoint JSON files are disposable projections."""

import hashlib
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import storage

ACTIVE = ContextVar("checkpoint_transaction", default=None)
ACTOR = ContextVar("checkpoint_actor", default={})
MAX_BATCH_BYTES = 16 * 1024 * 1024
TRACKED = (
    "goal",
    "next_action",
    "status",
    "decisions",
    "verification",
    "blocker",
    "blocker_history",
    "artifacts",
    "head",
    "branch",
    "session_id",
    "platform",
    "parent_task_id",
    "outcome",
    "close_reason",
)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def record_revision(record):
    if record is None:
        return None
    return hashlib.sha256(
        canonical({k: v for k, v in record.items() if not k.startswith("_")})
    ).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def history_error(message):
    return ValueError(
        f"invalid checkpoint history: {message}; restore a consistent backup, do not edit committed events"
    )


def actor_values(supplied=None):
    value = {
        "session_id": os.environ.get("AGENT_CHECKPOINT_SESSION_ID"),
        "agent_id": os.environ.get("AGENT_CHECKPOINT_AGENT_ID"),
        **ACTOR.get(),
        **{k: v for k, v in (supplied or {}).items() if v is not None and v != ""},
    }
    return {
        k: storage.sanitized(v)[:200]
        for k, v in value.items()
        if k in ("session_id", "agent_id", "platform") and isinstance(v, str) and v
    }


@contextmanager
def actor_context(**values):
    token = ACTOR.set({**ACTOR.get(), **{k: v for k, v in values.items() if v}})
    try:
        yield
    finally:
        ACTOR.reset(token)


def _event_paths(state):
    directory = state / "events"
    storage.reject_link(directory)
    if not directory.exists():
        return []
    paths = []
    for path in directory.iterdir():
        if path.name.startswith("."):
            continue  # orphan exclusive temporary files are never committed
        if not re.fullmatch(r"[0-9]{20}\.json", path.name):
            raise history_error("unexpected event filename")
        storage.reject_link(path)
        if not path.is_file():
            raise history_error("event is not a regular file")
        paths.append(path)
    return sorted(paths)


def _validate_observations(batch, before, after, attempts):
    pending = dict(attempts)
    for observation in batch["observations"]:
        if not isinstance(observation, dict):
            raise history_error("invalid observation")
        if (
            type(observation.get("event_version")) is not int
            or observation.get("event_version") != 1
        ):
            raise history_error("unsupported event version")
        kind = observation.get("type")
        identifier = observation.get("task_id")
        if kind not in (
            "task.imported",
            "task.created",
            "task.saved",
            "goal.changed",
            "decision.recorded",
            "blocker.changed",
            "task.completed",
            "task.closed",
            "task.reopened",
            "task.resumed",
            "task.handoff",
            "task.linked",
            "verification.started",
            "verification.finished",
            "verification.conflict",
            "verification.aborted",
        ):
            raise history_error("unsupported event type")
        if identifier not in after:
            raise history_error("observation refers to a missing task")
        details = observation.get("details")
        if not isinstance(details, dict) or not isinstance(
            observation.get("actor"), dict
        ):
            raise history_error("invalid observation fields")
        if any(
            k not in ("session_id", "agent_id", "platform") or not isinstance(v, str)
            for k, v in observation["actor"].items()
        ):
            raise history_error("invalid actor fields")
        try:
            if datetime.fromisoformat(observation["time"]).tzinfo is None:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise history_error("invalid observation time")
        if kind in ("task.created", "task.imported"):
            record = details.get("record")
            if not isinstance(record, dict) or record.get("id") != identifier:
                raise history_error("invalid baseline/create observation")
            if not any(
                change["record"] == record
                and change.get("imported", False) == (kind == "task.imported")
                for change in batch["changes"]
            ):
                raise history_error("observation does not match committed record")
        elif kind == "goal.changed":
            if not all(
                isinstance(details.get(key), str) for key in ("before", "after")
            ):
                raise history_error("invalid goal-change observation")
            if not any(
                change["record"]["id"] == identifier
                and change["record"]["goal"] == details["after"]
                for change in batch["changes"]
            ):
                raise history_error("goal-change observation does not match record")
        elif kind == "task.saved":
            delta = details.get("changes")
            if not isinstance(delta, dict) or any(
                key not in TRACKED
                or not isinstance(value, dict)
                or set(value) != {"before", "after"}
                for key, value in delta.items()
            ):
                raise history_error("invalid saved-task changes")
        elif kind == "decision.recorded":
            if not isinstance(details.get("decision"), str) or details[
                "decision"
            ] not in after[identifier].get("decisions", []):
                raise history_error("invalid recorded decision")
        elif kind == "blocker.changed":
            if not all(
                isinstance(details.get(key), str) for key in ("before", "after")
            ):
                raise history_error("invalid blocker observation")
        elif kind in ("task.completed", "task.closed", "task.reopened"):
            status = {
                "task.completed": "complete",
                "task.closed": "closed",
                "task.reopened": "open",
            }[kind]
            if details.get("status") != status or after[identifier]["status"] != status:
                raise history_error("status observation does not match record")
        elif kind == "task.handoff":
            if (
                not isinstance(details.get("to_agent"), str)
                or not details["to_agent"]
                or not all(
                    isinstance(details.get(key), str) for key in ("to_session", "note")
                )
            ):
                raise history_error("invalid handoff")
        elif kind == "task.linked":
            if (
                details.get("parent_task_id") not in after
                or after[identifier].get("parent_task_id") != details["parent_task_id"]
            ):
                raise history_error("invalid task link")
        if not kind.startswith("verification."):
            continue
        attempt = details.get("attempt_id")
        if not isinstance(attempt, str) or not re.fullmatch("[a-f0-9]{32}", attempt):
            raise history_error("invalid attempt ID")
        current = before.get(identifier) or after.get(identifier)
        if kind == "verification.started":
            if attempt in pending:
                raise history_error("duplicate attempt ID")
            if details.get("record_revision") != record_revision(current):
                raise history_error("verification start revision mismatch")
            pending[attempt] = {
                "task_id": identifier,
                "record_revision": details["record_revision"],
                "finished": False,
            }
        else:
            started = pending.get(attempt)
            if not started or started["finished"] or started["task_id"] != identifier:
                raise history_error("verification result without unique start")
            if type(details.get("applied")) is not bool:
                raise history_error("missing verification applicability")
            code = details.get("exit_code")
            if code is not None and type(code) is not int:
                raise history_error("invalid verification exit code")
            duration = details.get("duration_ns")
            if type(duration) is not int or duration < 0:
                raise history_error("invalid verification duration")
            if details["applied"]:
                if kind != "verification.finished" or started[
                    "record_revision"
                ] != record_revision(current):
                    raise history_error("applied verification revision mismatch")
            pending[attempt] = {**started, "finished": True}
    return pending


def replay(state):
    """Validate the entire committed prefix before making any state visible."""
    records = {}
    observations = []
    attempts = {}
    sequence = 0
    previous = ""
    batch_ids = set()
    store_id = None
    paths = _event_paths(state)
    for path in paths:
        try:
            if path.stat().st_size > MAX_BATCH_BYTES:
                raise history_error("event batch exceeds 16 MiB")
            raw = path.read_bytes()
            batch = json.loads(raw)
        except (OSError, ValueError, RecursionError) as exc:
            if isinstance(exc, ValueError) and "checkpoint history" in str(exc):
                raise
            raise history_error(f"unreadable batch {path.name}") from exc
        if (
            not isinstance(batch, dict)
            or type(batch.get("schema_version")) is not int
            or batch.get("schema_version") != 1
        ):
            raise history_error("unsupported batch schema")
        if (
            type(batch.get("sequence")) is not int
            or batch["sequence"] != sequence + 1
            or path.stem != f"{sequence + 1:020d}"
        ):
            raise history_error("missing or reordered sequence")
        if batch.get("previous_sha256") != previous:
            raise history_error("previous batch hash mismatch")
        identifier = batch.get("batch_id")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch("[a-f0-9]{32}", identifier)
            or identifier in batch_ids
        ):
            raise history_error("duplicate or invalid batch ID")
        sid = batch.get("store_id")
        if (
            not isinstance(sid, str)
            or not re.fullmatch("[a-f0-9]{32}", sid)
            or (store_id and sid != store_id)
        ):
            raise history_error("invalid store identity")
        if (
            not isinstance(batch.get("operation"), str)
            or not isinstance(batch.get("changes"), list)
            or not isinstance(batch.get("observations"), list)
        ):
            raise history_error("invalid batch fields")
        try:
            if datetime.fromisoformat(batch["time"]).tzinfo is None:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise history_error("invalid batch time")
        updated = dict(records)
        for change in batch["changes"]:
            if not isinstance(change, dict) or not isinstance(
                change.get("record"), dict
            ):
                raise history_error("invalid record change")
            record = change["record"]
            task_id = record.get("id")
            try:
                storage.validate_id(task_id)
                storage.validate_record(
                    record, state / "checkpoints" / f"{task_id}.json"
                )
            except ValueError as exc:
                raise history_error("invalid task record") from exc
            old = updated.get(task_id)
            if change.get("before_revision") != record_revision(old):
                raise history_error("task revision mismatch")
            imported = change.get("imported", False)
            if type(imported) is not bool:
                raise history_error("invalid baseline flag")
            if imported and (sequence != 0 or old is not None):
                raise history_error("baseline after history began")
            if (
                not imported
                and old
                and old["status"] != "open"
                and record["status"] != "open"
            ):
                raise history_error("invalid terminal transition")
            if not imported and old is None and record["status"] != "open":
                raise history_error("new task is not open")
            updated[task_id] = deepcopy(record)
        for change in batch["changes"]:
            current = change["record"]
            seen = {current["id"]}
            while current.get("parent_task_id"):
                parent = current["parent_task_id"]
                if parent not in updated or parent in seen:
                    raise history_error("missing or cyclic parent task")
                seen.add(parent)
                current = updated[parent]
        attempts = _validate_observations(batch, records, updated, attempts)
        records = updated
        sequence += 1
        store_id = sid
        batch_ids.add(identifier)
        for index, event in enumerate(batch["observations"]):
            observations.append(
                {
                    **event,
                    "event_id": f"{identifier}:{index}",
                    "sequence": sequence,
                    "index": index,
                    "cursor": f"{sequence}.{index}",
                    "store_id": sid,
                    "batch_id": identifier,
                }
            )
        previous = hashlib.sha256(raw).hexdigest()
    return records, observations, sequence, previous, store_id


def _legacy(state):
    from .core import slugify

    directory = state / "checkpoints"
    storage.reject_link(directory)
    records = {}
    source_hashes = {}
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            storage.reject_link(path)
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("legacy checkpoint exceeds 8 MiB; inspect it manually")
            raw = path.read_bytes()
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ValueError(
                    f"corrupt checkpoint: {path}; restore a backup"
                ) from exc
            storage.validate_record(record, path)
            record.setdefault("name", slugify(record["goal"]))
            records[record["id"]] = storage.sanitized(record)
            source_hashes[record["id"]] = hashlib.sha256(raw).hexdigest()
    return records, source_hashes


def refresh_projection(state, records):
    for identifier, record in records.items():
        storage.write_record_file(state / "checkpoints" / f"{identifier}.json", record)


class Transaction:
    def __init__(self, state, operation, actor=None):
        self.state = Path(state).expanduser().absolute()
        self.operation = operation
        self.actor = actor_values(actor)
        self.records, self.events, self.sequence, self.previous, self.store_id = replay(
            self.state
        )
        self.baselines = {}
        self.baseline_source_hashes = {}
        if not self.sequence:
            self.records, self.baseline_source_hashes = _legacy(self.state)
            self.baselines = deepcopy(self.records)
        self.original = deepcopy(self.records)
        self.changes = []
        self.observations = []
        self.attempts = {}
        for event in self.events:
            details = event["details"]
            if event["type"] == "verification.started":
                self.attempts[details["attempt_id"]] = {
                    "task_id": event["task_id"],
                    "record_revision": details["record_revision"],
                    "finished": False,
                }
            elif event["type"] in (
                "verification.finished",
                "verification.conflict",
                "verification.aborted",
            ):
                self.attempts[details["attempt_id"]]["finished"] = True
        self.store_id = self.store_id or uuid.uuid4().hex

    def observe(self, kind, task_id, details=None, time=None, actor=None):
        self.observations.append(
            {
                "event_version": 1,
                "type": kind,
                "task_id": task_id,
                "time": time or utcnow(),
                "actor": self.actor if actor is None else actor_values(actor),
                "details": storage.sanitized(details or {}),
            }
        )

    def write(self, path, record):
        if Path(path).parent != self.state / "checkpoints":
            raise ValueError("checkpoint path is outside transaction state")
        record = storage.sanitized(record)
        storage.validate_record(record, Path(path))
        identifier = record["id"]
        before = self.records.get(identifier)
        self.changes.append(
            {
                "before_revision": record_revision(before),
                "record": deepcopy(record),
                "imported": False,
            }
        )
        self.records[identifier] = record
        if before is None:
            self.observe("task.created", identifier, {"record": record})
        else:
            delta = {
                key: {"before": before.get(key), "after": record.get(key)}
                for key in TRACKED
                if before.get(key) != record.get(key)
            }
            self.observe("task.saved", identifier, {"changes": delta})
            if before["goal"] != record["goal"]:
                self.observe("goal.changed", identifier, delta["goal"])
            for decision in record.get("decisions", []):
                if decision not in before.get("decisions", []):
                    self.observe(
                        "decision.recorded", identifier, {"decision": decision}
                    )
            if before.get("blocker", "") != record.get("blocker", ""):
                self.observe(
                    "blocker.changed",
                    identifier,
                    {
                        "before": before.get("blocker", ""),
                        "after": record.get("blocker", ""),
                    },
                )
            if before["status"] != record["status"]:
                kind = {
                    "open": "task.reopened",
                    "closed": "task.closed",
                    "complete": "task.completed",
                }[record["status"]]
                checks = record.get("verification", [])
                mode = (
                    "executed"
                    if self.operation == "verification.finish"
                    else "manual"
                    if checks
                    and isinstance(checks[-1], dict)
                    and checks[-1].get("status") == "manual"
                    else "caller-reported"
                )
                self.observe(
                    kind,
                    identifier,
                    {
                        "status": record["status"],
                        "outcome": record.get("outcome", ""),
                        "verification_mode": mode,
                    },
                )
        return dict(record, _path=str(path))

    def commit(self):
        if not self.changes and not self.observations:
            return
        now = utcnow()
        baselines = []
        imports = []
        if not self.sequence:
            for identifier, record in self.baselines.items():
                baselines.append(
                    {"before_revision": None, "record": record, "imported": True}
                )
                imports.append(
                    {
                        "event_version": 1,
                        "type": "task.imported",
                        "task_id": identifier,
                        "time": now,
                        "actor": {},
                        "details": {
                            "record": record,
                            "imported_record_sha256": record_revision(record),
                            "source_file_sha256": self.baseline_source_hashes[
                                identifier
                            ],
                            "coverage": "baseline-only",
                        },
                    }
                )
        batch_id = uuid.uuid4().hex
        sequence = self.sequence + 1
        batch = {
            "schema_version": 1,
            "store_id": self.store_id,
            "sequence": sequence,
            "previous_sha256": self.previous,
            "batch_id": batch_id,
            "time": now,
            "operation": self.operation,
            "changes": baselines + self.changes,
            "observations": imports + self.observations,
        }
        data = canonical(batch) + b"\n"
        if len(data) > MAX_BATCH_BYTES:
            raise ValueError(
                "history batch exceeds 16 MiB; reduce transaction or migrate a smaller legacy state directory"
            )
        # Validate the same semantic invariants before persistence. Imported baselines
        # supply the starting state for the first verification observation.
        _validate_observations(batch, self.original, self.records, self.attempts)
        directory = self.state / "events"
        storage.ensure_private_dir(directory)
        target = directory / f"{sequence:020d}.json"
        if target.exists() or target.is_symlink():
            raise history_error("sequence already exists")
        try:
            storage.atomic_write(target, data.decode())
        except OSError as exc:
            if target.exists():
                raise RuntimeError(
                    f"commit durability uncertain for history sequence {sequence}, batch {batch_id}; inspect history --json before retrying, visibility does not prove power-loss durability"
                ) from exc
            raise
        # Reads reconstruct from history, so a failed cache update cannot invalidate
        # the successful durable event commit.
        try:
            changed_ids = {
                change["record"]["id"] for change in baselines + self.changes
            }
            refresh_projection(
                self.state,
                {identifier: self.records[identifier] for identifier in changed_ids},
            )
        except (OSError, ValueError):
            print(
                "agent-checkpoint: history committed; projection refresh failed; reads will replay history",
                file=sys.stderr,
            )


@contextmanager
def transaction(state, operation, actor=None):
    if ACTIVE.get() is not None:
        raise RuntimeError("nested checkpoint transaction")
    with storage.locked(state):
        tx = Transaction(state, operation, actor)
        token = ACTIVE.set(tx)
        try:
            yield tx
            tx.commit()
        finally:
            ACTIVE.reset(token)
