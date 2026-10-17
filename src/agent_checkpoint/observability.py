"""Task trajectories and measured outcome reports from committed observations."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .git import repo_identity, same_repo
from .journal import transaction


def cursor_value(value):
    if value is None:
        return (0, -1)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", value):
        raise ValueError("invalid history cursor; use the returned next_cursor")
    return tuple(map(int, value.split(".")))


def snapshot(state):
    with transaction(state, "observe") as tx:
        return tx.records, tx.events, tx.sequence


def selected(
    records, events, ref=None, cwd=None, since_days=None, session_id=None, agent_id=None
):
    identifiers = set(records)
    if ref is not None:
        matches = [record for record in records.values() if record["id"] == ref]
        if not matches:
            matches = [
                record
                for record in records.values()
                if record.get("name") == ref and record["status"] == "open"
            ]
            if not matches:
                matches = [
                    record
                    for record in records.values()
                    if record.get("name") == ref or record["id"].startswith(ref)
                ]
        if len(matches) != 1:
            raise ValueError(
                "history task reference is missing or ambiguous; use the full task ID"
            )
        identifiers = {matches[0]["id"]}
    if cwd is not None:
        identity = repo_identity(Path(cwd))
        identifiers = {
            identifier
            for identifier in identifiers
            if same_repo(records[identifier], identity)
        }
    cutoff = None
    if since_days is not None:
        if type(since_days) is not int or since_days < 0:
            raise ValueError("since days must be nonnegative")
        if since_days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    result = []
    for event in events:
        if event["task_id"] not in identifiers:
            continue
        if cutoff and datetime.fromisoformat(event["time"]) < cutoff:
            continue
        if session_id and event["actor"].get("session_id") != session_id:
            continue
        if agent_id and event["actor"].get("agent_id") != agent_id:
            continue
        result.append(event)
    return result, identifiers, cutoff


def coverage(records, events, sequence, filtered_events=None):
    events = [event for event in events if event["task_id"] in records]
    filtered_events = events if filtered_events is None else filtered_events
    imports = [e for e in events if e["type"] == "task.imported"]
    starts = {
        e["details"]["attempt_id"]
        for e in events
        if e["type"] == "verification.started"
    }
    finishes = {
        e["details"]["attempt_id"]
        for e in events
        if e["type"]
        in ("verification.finished", "verification.conflict", "verification.aborted")
    }
    return {
        "scope": "selected tasks across their complete recorded history",
        "filtered_event_count": len(filtered_events),
        "filtered_events_without_session": sum(
            not e["actor"].get("session_id") for e in filtered_events
        ),
        "filtered_events_without_agent": sum(
            not e["actor"].get("agent_id") for e in filtered_events
        ),
        "committed_sequence": sequence,
        "history_started": bool(sequence),
        "imported_baselines": len(imports),
        "legacy_records_without_events": len(records) if not sequence else 0,
        "events_without_session": sum(not e["actor"].get("session_id") for e in events),
        "events_without_agent": sum(not e["actor"].get("agent_id") for e in events),
        "incomplete_verification_attempts": len(starts - finishes),
        "pre_import_history": "unavailable"
        if imports or not sequence
        else "not-imported",
    }


def history(
    state,
    ref=None,
    limit=100,
    after=None,
    cwd=None,
    since_days=None,
    session_id=None,
    agent_id=None,
    _snapshot=None,
):
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("history limit must be between 1 and 10000")
    cursor = cursor_value(after)
    records, events, sequence = _snapshot if _snapshot is not None else snapshot(state)
    filtered, identifiers, _ = selected(
        records, events, ref, cwd, since_days, session_id, agent_id
    )
    remaining = [e for e in filtered if (e["sequence"], e["index"]) > cursor]
    page = remaining[:limit]
    return {
        "events": page,
        "next_cursor": page[-1]["cursor"] if page else after,
        "has_more": len(remaining) > limit,
        "coverage": coverage(
            {i: records[i] for i in identifiers}, events, sequence, filtered
        ),
    }


def render_timeline(page):
    lines = []
    for event in page["events"]:
        actor = event["actor"]
        identity = (
            " / ".join(v for v in (actor.get("session_id"), actor.get("agent_id")) if v)
            or "actor unknown"
        )
        lines.append(
            f"{event['time']}  {event['task_id']}  {event['type']}  [{identity}]"
        )
        details = event["details"]
        if event["type"] in ("task.created", "task.imported"):
            lines.append("  Goal: " + details["record"]["goal"])
        elif event["type"] == "goal.changed":
            lines.append(f"  {details['before']} -> {details['after']}")
        elif event["type"] == "task.saved":
            for name, change in details.get("changes", {}).items():
                if name in ("goal", "next_action", "blocker", "status"):
                    lines.append(
                        f"  {name}: {change.get('before')} -> {change.get('after')}"
                    )
        elif event["type"] == "decision.recorded":
            lines.append("  Decision: " + details["decision"])
        elif event["type"].startswith("verification."):
            lines.append("  " + json.dumps(details, sort_keys=True))
        elif details:
            lines.append("  " + json.dumps(details, sort_keys=True))
    if not lines:
        lines.append("No recorded history matches.")
    if page["has_more"]:
        lines.append("More events: continue with --after " + page["next_cursor"])
    if page["coverage"]["imported_baselines"]:
        lines.append("Coverage: imported baseline; earlier revisions are unavailable.")
    return "\n".join(lines)


def stats(state, cwd=None, since_days=30):
    records, events, sequence = snapshot(state)
    filtered, identifiers, cutoff = selected(
        records, events, cwd=cwd, since_days=since_days
    )
    visible = {i: records[i] for i in identifiers}
    now = datetime.now(timezone.utc)
    counts = {
        kind: sum(e["type"] == kind for e in filtered)
        for kind in (
            "task.created",
            "goal.changed",
            "task.reopened",
            "task.handoff",
            "task.completed",
            "task.closed",
            "verification.started",
            "verification.finished",
            "verification.conflict",
            "verification.aborted",
        )
    }
    results = [e for e in filtered if e["type"] == "verification.finished"]
    counts["verification.passed"] = sum(e["details"]["exit_code"] == 0 for e in results)
    counts["verification.failed"] = sum(e["details"]["exit_code"] != 0 for e in results)
    completions = [e for e in filtered if e["type"] == "task.completed"]
    counts["completion.verified"] = sum(
        e["details"].get("verification_mode") == "executed" for e in completions
    )
    counts["completion.manual"] = sum(
        e["details"].get("verification_mode") == "manual" for e in completions
    )
    counts["completion.caller_reported"] = sum(
        e["details"].get("verification_mode") == "caller-reported" for e in completions
    )
    blocked = {}
    status = {}
    text = {}
    blocked_seconds = 0.0
    unknown = set()
    anomalies = []
    created = {}
    elapsed = []

    def interval(identifier, end):
        nonlocal blocked_seconds
        start = blocked.pop(identifier, None)
        if start is None:
            return
        if end < start:
            anomalies.append({"task_id": identifier, "kind": "clock-reversal"})
            return
        overlap = max(start, cutoff) if cutoff else start
        blocked_seconds += max(0, (end - overlap).total_seconds())

    for event in events:
        identifier = event["task_id"]
        if identifier not in identifiers:
            continue
        when = datetime.fromisoformat(event["time"])
        kind = event["type"]
        details = event["details"]
        if kind in ("task.created", "task.imported"):
            record = details["record"]
            status[identifier] = record["status"]
            text[identifier] = record.get("blocker", "")
            if kind == "task.created":
                created[identifier] = when
            if record["status"] == "open" and text[identifier]:
                blocked[identifier] = when
                if kind == "task.imported":
                    unknown.add(identifier)
        elif kind == "blocker.changed":
            interval(identifier, when)
            text[identifier] = details["after"]
            if text[identifier] and status.get(identifier) == "open":
                blocked[identifier] = when
        elif kind in ("task.completed", "task.closed"):
            interval(identifier, when)
            status[identifier] = details["status"]
            if (
                kind == "task.completed"
                and identifier in created
                and (cutoff is None or when >= cutoff)
            ):
                duration = (when - created[identifier]).total_seconds()
                if duration < 0:
                    anomalies.append({"task_id": identifier, "kind": "clock-reversal"})
                else:
                    elapsed.append(duration)
        elif kind == "task.reopened":
            status[identifier] = "open"
            if text.get(identifier):
                blocked[identifier] = when
    for identifier in list(blocked):
        interval(identifier, now)
    return {
        "window_days": since_days,
        "counts": counts,
        "current_tasks": {
            state: sum(r["status"] == state for r in visible.values())
            for state in ("open", "complete", "closed")
        },
        "blocked_wall_seconds": blocked_seconds,
        "blocked_duration_lower_bound_tasks": sorted(unknown),
        "elapsed_to_completion_seconds": elapsed,
        "verification_run_seconds": sum(
            e["details"]["duration_ns"] / 1e9
            for e in filtered
            if e["type"]
            in (
                "verification.finished",
                "verification.conflict",
                "verification.aborted",
            )
        ),
        "clock_anomalies": anomalies,
        "coverage": coverage(visible, events, sequence, filtered),
        "measurement": "Recorded wall time and command duration, not active effort or semantic quality.",
    }
