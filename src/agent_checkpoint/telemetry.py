"""Explicit OTLP/HTTP JSON export using only the Python standard library."""

import hashlib
import ipaddress
import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import __version__
from .observability import history, snapshot
from .storage import sanitized

MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


def digest(value, length=32):
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def attribute(key, value):
    if isinstance(value, bool):
        typed = {"boolValue": value}
    elif isinstance(value, int):
        typed = {"intValue": str(value)}
    else:
        typed = {"stringValue": str(value)}
    return {"key": key, "value": typed}


def nanos(value):
    dt = datetime.fromisoformat(value).astimezone(timezone.utc) - datetime(
        1970, 1, 1, tzinfo=timezone.utc
    )
    return (dt.days * 86400 + dt.seconds) * 1000000000 + dt.microseconds * 1000


def identifiers(event):
    key = event["store_id"] + "/" + event["task_id"]
    return digest(key), digest(event["event_id"], 16)


def attributes(event):
    attrs = [
        attribute("agent_checkpoint.event.id", event["event_id"]),
        attribute("agent_checkpoint.event.type", event["type"]),
        attribute("agent_checkpoint.sequence", event["sequence"]),
        attribute("agent_checkpoint.task.id", identifiers(event)[0]),
    ]
    for key in ("session_id", "agent_id"):
        if event["actor"].get(key):
            attrs.append(
                attribute("agent_checkpoint." + key, digest(event["actor"][key]))
            )
    return attrs


def payload(page, signal, all_events, include_content):
    resource = {
        "attributes": [
            attribute("service.name", "agent-checkpoint"),
            attribute("service.version", __version__),
        ]
    }
    scope = {"name": "agent-checkpoint", "version": __version__}
    if signal == "logs":
        logs = []
        for event in page["events"]:
            trace, span = identifiers(event)
            item = {
                "timeUnixNano": str(nanos(event["time"])),
                "observedTimeUnixNano": str(nanos(event["time"])),
                "severityNumber": 9,
                "severityText": "INFO",
                "body": {"stringValue": event["type"]},
                "attributes": attributes(event),
                "traceId": trace,
            }
            if event["details"].get("attempt_id"):
                item["spanId"] = digest(event["details"]["attempt_id"], 16)
            if include_content:
                item["body"] = {
                    "stringValue": json.dumps(
                        sanitized(event["details"]), sort_keys=True
                    )
                }
            logs.append(item)
        return {
            "resourceLogs": [
                {
                    "resource": resource,
                    "scopeLogs": [{"scope": scope, "logRecords": logs}],
                }
            ]
        }
    starts = {
        e["details"]["attempt_id"]: e
        for e in all_events
        if e["type"] == "verification.started"
    }
    spans = []
    for event in page["events"]:
        if event["type"] not in (
            "verification.finished",
            "verification.conflict",
            "verification.aborted",
        ):
            continue
        details = event["details"]
        start = starts.get(details["attempt_id"])
        if start is None:
            continue
        stamp = nanos(start["time"])
        attrs = attributes(event) + [
            attribute("agent_checkpoint.verification.applied", details["applied"])
        ]
        if details.get("exit_code") is not None:
            attrs.append(attribute("process.exit.code", details["exit_code"]))
        if include_content:
            attrs.append(
                attribute(
                    "agent_checkpoint.command", sanitized(start["details"]["command"])
                )
            )
        spans.append(
            {
                "traceId": identifiers(event)[0],
                "spanId": digest(details["attempt_id"], 16),
                "name": "agent_checkpoint.verify",
                "kind": 1,
                "startTimeUnixNano": str(stamp),
                "endTimeUnixNano": str(stamp + details["duration_ns"]),
                "attributes": attrs,
                "status": {
                    "code": 1
                    if details.get("exit_code") == 0 and details["applied"]
                    else 2
                },
            }
        )
    return {
        "resourceSpans": [
            {"resource": resource, "scopeSpans": [{"scope": scope, "spans": spans}]}
        ]
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError(
            "OTLP redirects are disabled; configure the collector endpoint directly"
        )


def send(endpoint, body, timeout=10, allow_insecure=False):
    parts = urlsplit(endpoint)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "OTLP endpoint must be an HTTP(S) URL without credentials, query or fragment"
        )
    loopback = parts.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme == "http" and not loopback and not allow_insecure:
        raise ValueError(
            "non-loopback OTLP endpoints require HTTPS or explicit --allow-insecure"
        )
    if not 0 < timeout <= 60:
        raise ValueError("export timeout must be between 0 and 60 seconds")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    for entry in filter(
        None, os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(",")
    ):
        if "=" not in entry:
            raise ValueError("invalid OTLP headers environment variable")
        key, value = entry.split("=", 1)
        if not key.strip() or "\n" in key + value or "\r" in key + value:
            raise ValueError("invalid OTLP header")
        headers[key.strip()] = unquote(value.strip())
    opener = build_opener(NoRedirect(), ProxyHandler({}))
    try:
        with opener.open(
            Request(endpoint, data=body, headers=headers, method="POST"),
            timeout=timeout,
        ) as response:
            if response.status != 200:
                raise RuntimeError("OTLP collector returned a non-success status")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise RuntimeError(
            f"OTLP export failed with HTTP {exc.code}; local history is unchanged"
        ) from None
    except (URLError, OSError, TimeoutError, ValueError):
        raise RuntimeError(
            "OTLP transport failed; local history is unchanged"
        ) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("OTLP response exceeds the size limit")
    try:
        result = json.loads(raw or b"{}")
    except (ValueError, RecursionError):
        raise RuntimeError("OTLP collector returned malformed JSON") from None
    if not isinstance(result, dict):
        raise RuntimeError("OTLP collector returned an invalid response")
    partial = result.get("partialSuccess")
    if partial:
        raise RuntimeError(
            "OTLP collector reported partial success; not retried, local history is unchanged"
        )
    return {"sent": True}


def export(
    state,
    signal="logs",
    include_content=False,
    endpoint=None,
    timeout=10,
    allow_insecure=False,
    **filters,
):
    if signal not in ("logs", "traces"):
        raise ValueError("export signal must be logs or traces")
    captured = snapshot(state)
    page = history(state, _snapshot=captured, **filters)
    _, all_events, _ = captured
    data = payload(page, signal, all_events, include_content)
    body = json.dumps(data, separators=(",", ":")).encode()
    if len(body) > MAX_EXPORT_BYTES:
        raise ValueError(
            "OTLP body exceeds 8 MiB; use a narrower filter or smaller --limit"
        )
    result = {
        "payload": data,
        "next_cursor": page["next_cursor"],
        "has_more": page["has_more"],
        "coverage": page["coverage"],
    }
    if endpoint:
        result["delivery"] = send(endpoint, body, timeout, allow_insecure)
    return result
