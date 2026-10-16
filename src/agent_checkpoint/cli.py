#!/usr/bin/env python3
"""Save, inspect, resume, verify, close, and complete local agent task checkpoints, and render a work log."""

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import service as hc

REF_HELP = "checkpoint name, id, or unique id prefix"


def emit(value, context=False, token_limit=2000):
    value = hc.sanitized(value)
    if context:
        print(hc.checkpoint_context(value, token_limit))
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def age(timestamp):
    try:
        then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    seconds = (datetime.now(UTC) - then).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"


def display_ref(record, open_names, all_names):
    """Show the name when it resolves uniquely, else the id."""
    counts = open_names if record.get("status") == "open" else all_names
    return str(
        record.get("name") if counts[record.get("name")] == 1 else record.get("id")
    )


def print_table(records):
    records = hc.sanitized(records)
    if not records:
        print("no checkpoints")
        return
    open_names = Counter(r.get("name") for r in records if r.get("status") == "open")
    all_names = Counter(r.get("name") for r in records)
    rows = []
    for r in records:
        status = str(r.get("status", ""))
        if r.get("close_reason"):
            status = r["close_reason"]
        flags = []
        if hc.stale_checks(r):
            flags.append(f"{hc.stale_checks(r)} stale check")
        if r.get("blocker") and r.get("status") == "open":
            flags.append("blocked")
        rows.append(
            (
                display_ref(r, open_names, all_names),
                status,
                age(r.get("updated_at")),
                Path(str(r.get("repo", ""))).name,
                (f"[{', '.join(flags)}] " if flags else "")
                + " ".join(str(r.get("goal", "")).split())[:60],
            )
        )
    widths = [min(max(len(row[i]) for row in rows), 40) for i in range(4)]
    for row in rows:
        print(
            "  ".join(cell[:w].ljust(w) for cell, w in zip(row, widths)) + "  " + row[4]
        )


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir")
    sub = parser.add_subparsers(dest="command", required=True)

    save = sub.add_parser("save", help="create or update a checkpoint")
    save.add_argument(
        "--name", help="readable slug; saving again with the same name updates it"
    )
    save.add_argument("--goal", help="required for a new checkpoint")
    save.add_argument("--next", dest="next_action", required=True)
    save.add_argument("--decision", action="append", default=[])
    save.add_argument("--verification", action="append", default=[])
    blocker = save.add_mutually_exclusive_group()
    blocker.add_argument("--blocker", help="set the current blocker; omit to keep it")
    blocker.add_argument(
        "--clear-blocker", action="store_true", help="mark the blocker resolved"
    )
    save.add_argument(
        "--supersedes",
        action="append",
        default=[],
        metavar="REF",
        help="close an older checkpoint as superseded by this one",
    )
    save.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="PATH",
        help="where the work lives: a file, directory, PR, or URL",
    )
    save.add_argument("--cwd", default=".")
    save.add_argument("--session-id")

    listing = sub.add_parser("list", help="list checkpoints")
    listing.add_argument("--status", choices=("open", "complete", "closed"))
    listing.add_argument("--json", action="store_true", help="print full records")

    offer = sub.add_parser("offer", help="find the newest matching open checkpoints")
    offer.add_argument("--cwd", default=".")
    offer.add_argument(
        "--context", action="store_true", help="render the newest match for resumption"
    )
    offer.add_argument("--limit", type=int, default=3)

    resume = sub.add_parser("resume", help="render a checkpoint for resumption")
    resume.add_argument("id", help=REF_HELP)
    resume.add_argument("--context", action="store_true")

    verify = sub.add_parser(
        "verify", help="run a check and record the result; status stays open"
    )
    verify.add_argument("id", help=REF_HELP)
    verify.add_argument("--run", required=True)
    verify.add_argument("--cwd", default=".")

    complete = sub.add_parser("complete", help="mark a checkpoint complete")
    complete.add_argument("id", help=REF_HELP)
    complete.add_argument(
        "--run", help="run this verification command before completion"
    )
    complete.add_argument("--cwd", default=".")
    complete.add_argument("--manual", action="store_true")
    complete.add_argument(
        "--note", default="", help="one-line outcome for the work log"
    )
    complete.add_argument("--artifact", action="append", default=[], metavar="PATH")

    close = sub.add_parser("close", help="close checkpoints that will not be completed")
    close.add_argument("ids", nargs="+", metavar="id", help=REF_HELP)
    close.add_argument("--reason", choices=hc.CLOSE_REASONS, required=True)
    close.add_argument("--note", default="", help="one-line outcome for the work log")
    close.add_argument("--artifact", action="append", default=[], metavar="PATH")

    log = sub.add_parser(
        "log", help="render finished checkpoints as a markdown work log"
    )
    log.add_argument("--cwd", default=".")
    log.add_argument(
        "--all-repos", action="store_true", help="include every repository"
    )
    log.add_argument(
        "--since", type=int, default=90, metavar="DAYS", help="0 means no limit"
    )
    log.add_argument(
        "--include",
        action="append",
        choices=hc.FINISHED_REASONS,
        help="outcomes to include; default complete and done-elsewhere",
    )
    log.add_argument("--json", action="store_true")

    dedupe = sub.add_parser(
        "dedupe", help="close older open copies of the same named task"
    )
    dedupe.add_argument(
        "--apply", action="store_true", help="close them; default is a dry run"
    )

    capture = sub.add_parser(
        "capture", help="capture from a transcript lifecycle event"
    )
    capture.add_argument("--transcript", required=True)
    capture.add_argument("--cwd", default=".")
    capture.add_argument(
        "--exclude-prefix",
        action="append",
        default=[],
        help="ignore prompts beginning with this text (case insensitive)",
    )
    capture.add_argument(
        "--event", choices=("compact", "stop", "session-end"), required=True
    )

    args = parser.parse_args()
    project = Path(getattr(args, "cwd", "."))
    state, policy = hc.configuration(args.state_dir, project)
    tokens = policy["checkpoint"]["resume_context_tokens"]

    if args.command == "save":
        emit(
            hc.save_checkpoint(
                state,
                Path(args.cwd),
                args.goal or "",
                args.next_action,
                args.decision,
                args.verification,
                "" if args.clear_blocker else args.blocker,
                session_id=args.session_id,
                name=args.name,
                supersedes=args.supersedes,
                artifacts=args.artifact,
            )
        )
    elif args.command == "list":
        records = hc.list_checkpoints(state, args.status)
        if args.json:
            print(json.dumps(hc.sanitized(records), indent=2, sort_keys=True))
        else:
            print_table(records)
    elif args.command == "offer":
        max_age = int(policy["checkpoint"].get("max_offer_age_days", 30))
        records = hc.offer_checkpoints(state, Path(args.cwd), max_age, args.limit)
        if not records:
            print("null")
            return 1
        if args.context:
            emit(records[0], True, tokens)
        else:
            print(json.dumps(hc.sanitized(records), indent=2, sort_keys=True))
    elif args.command == "resume":
        emit(hc.resolve_checkpoint(state, args.id), args.context, tokens)
    elif args.command == "verify":
        record, code = hc.run_verification(state, args.id, args.run, Path(args.cwd))
        emit(record)
        return 0 if code == 0 else 1
    elif args.command == "complete":
        if bool(args.manual) == bool(args.run):
            raise ValueError("completion requires exactly one of --run or --manual")
        if args.manual:
            emit(
                hc.complete_checkpoint(
                    state, args.id, manual=True, note=args.note, artifacts=args.artifact
                )
            )
        else:
            record, code = hc.run_verification(
                state,
                args.id,
                args.run,
                Path(args.cwd),
                complete=True,
                note=args.note,
                artifacts=args.artifact,
            )
            emit(record)
            if code:
                raise RuntimeError(
                    f"verification failed with exit code {code}; checkpoint remains open"
                )
    elif args.command == "close":
        records = hc.close_many(
            state, args.ids, args.reason, note=args.note, artifacts=args.artifact
        )
        for record in records:
            print(
                f"closed {record['id']} ({args.reason}): {record.get('goal', '')[:60]}"
            )
    elif args.command == "dedupe":
        actions = hc.sanitized(hc.dedupe_checkpoints(state, apply=args.apply))
        verb = "closed" if args.apply else "would close"
        for action in actions:
            print(
                f"{verb} {action['close']}, keeping {action['keep']} ({action['name']})"
            )
        if not actions:
            print("no duplicate open checkpoints")
        elif not args.apply:
            print("dry run; pass --apply to close them")
    elif args.command == "log":
        entries = hc.work_log(
            state,
            None if args.all_repos else Path(args.cwd),
            args.since or None,
            tuple(args.include or ("complete", "done-elsewhere")),
        )
        if args.json:
            print(json.dumps(hc.sanitized(entries), indent=2, sort_keys=True))
        else:
            print(hc.render_work_log(hc.sanitized(entries)), end="")
    elif args.command == "capture":
        record = hc.capture_checkpoint(
            state,
            Path(args.transcript),
            Path(args.cwd),
            args.event,
            tuple(p.lower() for p in args.exclude_prefix),
        )
        emit(record)
    return 0


def main():
    try:
        return _main()
    except (OSError, ValueError, RuntimeError) as exc:
        print(hc.redact_secrets(f"agent-checkpoint: {exc}")[0], file=sys.stderr)
        return 1
