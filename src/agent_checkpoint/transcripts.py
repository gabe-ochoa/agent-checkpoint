"""Opt-in, bounded JSONL capture. No transcript text is executed."""

import json
from pathlib import Path

from . import core
from .redaction import redact_secrets
from .storage import locked

MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
INJECTED = (
    "<task-notification",
    "<local-command",
    "<system-reminder",
    "<environment_context",
    "<user_instructions",
    "# agents.md instructions",
    "this session is being continued",
    "the following is the codex agent history",
    ">>> approval request start",
    ">>> transcript delta start",
)


def text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict)
            and b.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def read_summary(path, exclude_prefixes=()):
    first = ""
    last = ""
    assistant = ""
    count = 0
    errors = 0
    aborted = False
    platform = ""
    session_id = path.stem
    excluded = False
    scheduled = False
    total = 0
    with path.open("rb") as handle:
        for number in range(1, 1000000):
            raw = handle.readline(MAX_EVENT_BYTES + 1)
            if not raw:
                break
            total += len(raw)
            if len(raw) > MAX_EVENT_BYTES or total > MAX_TRANSCRIPT_BYTES:
                raise ValueError(
                    "transcript exceeds capture size limit (32 MiB total, 1 MiB/event); split into a smaller transcript"
                )
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ValueError(
                    f"malformed transcript JSON at line {number}; repair or use a complete transcript"
                ) from exc
            if not isinstance(event, dict):
                raise ValueError(f"transcript event at line {number} must be an object")
            kind = event.get("type")
            prompt = ""
            reply = ""
            if kind == "session_meta":
                platform = "codex"
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("invalid transcript session metadata")
                session_id = str(
                    payload.get("session_id") or payload.get("id") or session_id
                )
                source = payload.get("source")
                excluded = (
                    excluded
                    or isinstance(source, dict)
                    or source == "exec"
                    or payload.get("originator") in ("codex_exec", "codex-exec")
                )
            if kind in ("user", "assistant", "system") and "message" in event:
                platform = platform or "claude"
            if platform == "claude":
                session_id = str(event.get("sessionId") or session_id)
                excluded = excluded or bool(
                    event.get("isSidechain") or event.get("agentId")
                )
                message = event.get("message", {})
                if not isinstance(message, dict):
                    raise ValueError("invalid transcript message")
                origin = event.get("origin", {})
                if not isinstance(origin, dict):
                    origin = {}
                if kind == "system" and event.get("subtype") == "scheduled_task_fire":
                    scheduled = True
                if kind == "user":
                    source = event.get("promptSource") or message.get("promptSource")
                    human = origin.get("kind") == "human" or source == "typed"
                    if human:
                        scheduled = False
                    automatic = not human and (
                        scheduled
                        or source in ("sdk", "system")
                        or event.get("entrypoint")
                        in ("sdk-cli", "sdk-ts", "sdk-python")
                    )
                    if (
                        not automatic
                        and not event.get("isMeta")
                        and not event.get("isCompactSummary")
                    ):
                        content = message.get("content")
                        if not (
                            isinstance(content, list)
                            and any(
                                isinstance(b, dict) and b.get("type") == "tool_result"
                                for b in content
                            )
                        ):
                            prompt = text(content)
                    if isinstance(message.get("content"), list):
                        errors += sum(
                            bool(b.get("is_error"))
                            for b in message["content"]
                            if isinstance(b, dict) and b.get("type") == "tool_result"
                        )
                elif kind == "assistant" and not scheduled:
                    reply = text(message.get("content"))
                aborted = aborted or event.get("subtype") in ("aborted", "turn_aborted")
            elif platform == "codex":
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("invalid transcript payload")
                subtype = payload.get("type")
                if kind == "event_msg" and subtype == "user_message":
                    prompt = text(payload.get("message"))
                elif kind == "event_msg" and subtype == "item_completed":
                    item = payload.get("item", {})
                    if isinstance(item, dict) and item.get("type") == "UserMessage":
                        prompt = text(item.get("content"))
                elif kind == "event_msg" and subtype == "agent_message":
                    reply = text(payload.get("message"))
                elif kind == "response_item" and subtype == "message":
                    if payload.get("role") == "user":
                        prompt = text(payload.get("content"))
                    elif payload.get("role") == "assistant":
                        reply = text(payload.get("content"))
                aborted = aborted or subtype == "turn_aborted"
            if prompt and not prompt.strip().lower().startswith(
                INJECTED + tuple(exclude_prefixes)
            ):
                prompt = core.clean_prompt(prompt)
                if prompt:
                    if prompt != last:
                        count += 1
                    first = first or prompt[:1000]
                    last = prompt[:1000]
            if reply:
                assistant = redact_secrets(reply)[0][:1000]
        else:
            raise ValueError(
                "transcript event count exceeds limit; use a smaller transcript"
            )
    if excluded:
        first = ""
    return dict(
        goal=first,
        last=last,
        assistant=assistant,
        count=count,
        errors=errors,
        aborted=aborted,
        platform=platform,
        session_id=session_id,
    )


def capture(state, transcript, cwd, event, exclude_prefixes=()):
    if event not in ("compact", "stop", "session-end"):
        raise ValueError("invalid capture event")
    session = read_summary(Path(transcript), exclude_prefixes)
    if not session["goal"]:
        return None
    if event == "stop" and not (
        session["aborted"] or session["errors"] or session["count"] > 1
    ):
        return None
    next_action = (
        "Last reply: " + core.first_sentence(session["assistant"])
        if session["assistant"]
        else "Respond to: " + core.first_sentence(session["last"])
    )
    with locked(state):
        identifier = core._checkpoint_id(
            core.git_snapshot(cwd)["repo"], session["session_id"]
        )
        existing = core._read_checkpoint(core._checkpoint_path(state, identifier))
        if existing and existing["status"] != "open":
            return existing
        return core.save_checkpoint(
            state,
            cwd,
            session["goal"],
            next_action,
            verification=[f"{session['errors']} tool error(s) remain"]
            if session["errors"]
            else [],
            blocker="turn aborted" if session["aborted"] else None,
            session_id=session["session_id"],
            platform=session["platform"],
            checkpoint_id=identifier,
        )
