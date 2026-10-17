#!/usr/bin/env python3
"""Measure full replay, memory and concurrent writer latency on synthetic history."""

import argparse
import hashlib
import json
import sys
import tempfile
import threading
import time
import tracemalloc
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_checkpoint import journal, service, storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batches", type=int, default=1000)
    parser.add_argument("--tasks", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.tasks <= args.batches:
        parser.error("tasks must be between 1 and batches")
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "state"
        cwd = Path(td)
        template = service.save_checkpoint(state, cwd, "Goal 0", "Next", name="task")
        template.pop("_path", None)
        first = next((state / "events").glob("*.json"))
        raw = first.read_bytes()
        batch = json.loads(raw)
        previous = hashlib.sha256(raw).hexdigest()
        records = {"task": template}
        for sequence in range(2, args.batches + 1):
            identifier = (
                "task" if args.tasks == 1 else "task-" + str(sequence % args.tasks)
            )
            before = records.get(identifier)
            after = deepcopy(before or template)
            after.update(id=identifier, name=identifier, goal=f"Goal {sequence}")
            batch = {
                "schema_version": 1,
                "store_id": batch["store_id"],
                "sequence": sequence,
                "previous_sha256": previous,
                "batch_id": uuid.uuid4().hex,
                "time": journal.utcnow(),
                "operation": "benchmark",
                "changes": [
                    {
                        "before_revision": journal.record_revision(before),
                        "record": after,
                        "imported": False,
                    }
                ],
                "observations": [
                    {
                        "event_version": 1,
                        "type": "goal.changed" if before else "task.created",
                        "task_id": identifier,
                        "time": journal.utcnow(),
                        "actor": {},
                        "details": {"before": before["goal"], "after": after["goal"]}
                        if before
                        else {"record": after},
                    }
                ],
            }
            raw = journal.canonical(batch) + b"\n"
            (state / "events" / f"{sequence:020d}.json").write_bytes(raw)
            previous = hashlib.sha256(raw).hexdigest()
            records[identifier] = after
        tracemalloc.start()
        start = time.monotonic()
        service.timeline(state, limit=100)
        elapsed = time.monotonic() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        entered = threading.Event()
        real_locked = storage.locked

        @contextmanager
        def observed_lock(*a, **kw):
            with real_locked(*a, **kw):
                if threading.current_thread().name == "history-benchmark":
                    entered.set()
                yield

        storage.locked = observed_lock
        failures = []

        def report():
            try:
                service.timeline(state, limit=100)
            except Exception as exc:
                failures.append(type(exc).__name__)

        thread = threading.Thread(target=report, name="history-benchmark")
        thread.start()
        if not entered.wait(10):
            raise RuntimeError("report did not acquire lock")
        start = time.monotonic()
        service.save_checkpoint(state, cwd, "After report", "Next", name="task")
        writer_seconds = time.monotonic() - start
        thread.join()
        storage.locked = real_locked
        if failures:
            raise RuntimeError("concurrent report failed")
        budget = 2 if args.batches <= 1000 else 15
        print(
            json.dumps(
                {
                    "batches": args.batches,
                    "tasks": len(records),
                    "report_seconds": elapsed,
                    "peak_bytes": peak,
                    "concurrent_writer_seconds": writer_seconds,
                    "budget_seconds": budget,
                    "memory_budget_bytes": 128 * 1024 * 1024,
                }
            )
        )
        if elapsed > budget or peak > 128 * 1024 * 1024 or writer_seconds > 15:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
