"""Independent reference oracle and journal contract, authored before implementation."""

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_checkpoint import journal
from agent_checkpoint import service as api


class ReplayOracle:
    def __init__(self):
        self.records = {}
        self.sequence = 0

    def apply(self, batch):
        if batch["sequence"] != self.sequence + 1:
            raise ValueError("sequence gap")
        pending = copy.deepcopy(self.records)
        for change in batch["changes"]:
            identifier = change["record"]["id"]
            before = pending.get(identifier)
            revision = journal.record_revision(before) if before else None
            if change["before_revision"] != revision:
                raise ValueError("revision mismatch")
            pending[identifier] = copy.deepcopy(change["record"])
        self.records = pending
        self.sequence += 1


class TrajectoryTests(unittest.TestCase):
    def test_generated_history_replays_against_independent_oracle(self):
        for seed in range(10):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as td:
                state = Path(td) / "state"
                cwd = Path(td)
                rng = random.Random(seed)
                api.save_checkpoint(state, cwd, "First goal", "Next", name="task")
                for index in range(30):
                    api.save_checkpoint(
                        state,
                        cwd,
                        f"Goal {rng.randrange(100)}",
                        f"Step {index}",
                        name="task",
                        decisions=[f"Decision {index}"],
                    )
                oracle = ReplayOracle()
                for path in sorted((state / "events").glob("*.json")):
                    oracle.apply(json.loads(path.read_text()))
                actual = api.resolve_checkpoint(state, "task")
                actual.pop("_path", None)
                self.assertEqual(oracle.records["task"], actual)
                page = api.history(state, "task", limit=10000)
                self.assertGreaterEqual(len(page["events"]), 31)
                self.assertIn("Goal", json.dumps(page))

    def test_history_survives_projection_failure(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            with mock.patch.object(
                journal,
                "refresh_projection",
                side_effect=OSError("injected cache failure"),
            ):
                api.save_checkpoint(state, cwd, "Committed goal", "Next", name="task")
            self.assertEqual(
                "Committed goal", api.resolve_checkpoint(state, "task")["goal"]
            )
            self.assertTrue((state / "events").exists())

    def test_stale_or_missing_cache_does_not_change_history(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            record = api.save_checkpoint(state, cwd, "Real goal", "Next", name="task")
            Path(record["_path"]).write_text("{broken cache")
            self.assertEqual("Real goal", api.resolve_checkpoint(state, "task")["goal"])
            Path(record["_path"]).unlink()
            self.assertEqual("Real goal", api.resolve_checkpoint(state, "task")["goal"])

    def test_legacy_baseline_is_labeled_and_read_does_not_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed = root / "seed"
            state = root / "legacy"
            record = api.save_checkpoint(seed, root, "Old goal", "Next", name="task")
            path = state / "checkpoints/task.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(Path(record["_path"]).read_bytes())
            api.resolve_checkpoint(state, "task")
            self.assertFalse((state / "events").exists())
            api.save_checkpoint(state, root, "New goal", "Next", name="task")
            page = api.history(state, "task")
            self.assertEqual("task.imported", page["events"][0]["type"])
            self.assertEqual("Old goal", page["events"][0]["details"]["record"]["goal"])

    def test_missing_or_corrupt_event_is_actionable(self):
        for corruption in ("missing", "json", "hash"):
            with (
                self.subTest(corruption=corruption),
                tempfile.TemporaryDirectory() as td,
            ):
                state = Path(td) / "state"
                cwd = Path(td)
                api.save_checkpoint(state, cwd, "Goal", "Next", name="task")
                api.save_checkpoint(state, cwd, "Goal 2", "Next", name="task")
                first = sorted((state / "events").glob("*.json"))[0]
                if corruption == "missing":
                    first.unlink()
                elif corruption == "json":
                    first.write_text("{bad")
                else:
                    record = json.loads(first.read_text())
                    record["operation"] = "tampered"
                    first.write_text(json.dumps(record))
                with self.assertRaisesRegex(ValueError, "history"):
                    api.resolve_checkpoint(state, "task")

    def test_atomic_batch_discards_partial_buffered_updates(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            for name in ("one", "two"):
                api.save_checkpoint(state, cwd, name, "Next", name=name)
            from agent_checkpoint import core

            real = core.close_checkpoint

            def close(*args, **kwargs):
                if args[1] == "two":
                    raise OSError("injected")
                return real(*args, **kwargs)

            with mock.patch.object(core, "close_checkpoint", side_effect=close):
                with self.assertRaises(RuntimeError):
                    api.close_many(state, ["one", "two"], "abandoned")
            self.assertEqual("open", api.resolve_checkpoint(state, "one")["status"])
            self.assertEqual("open", api.resolve_checkpoint(state, "two")["status"])

    def test_cursor_pagination_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Goal", "Next", name="task")
            for i in range(5):
                api.save_checkpoint(state, cwd, f"Goal {i}", f"Next {i}", name="task")
            complete = api.history(state, "task", limit=10000)["events"]
            seen = []
            cursor = None
            while True:
                page = api.history(state, "task", limit=2, after=cursor)
                seen.extend(page["events"])
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
            self.assertEqual(
                [e["event_id"] for e in complete], [e["event_id"] for e in seen]
            )

    def test_actor_handoff_parent_and_resume_are_observed(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Parent", "Next", name="parent")
            with api.actor_context(session_id="session-1", agent_id="agent-1"):
                api.save_checkpoint(state, cwd, "Child", "Next", name="child")
                api.link_checkpoint(state, "child", "parent")
                api.handoff_checkpoint(
                    state, "child", "agent-2", "session-2", "Review the change"
                )
                api.resume_checkpoint(state, "child")
            events = api.history(state, "child")["events"]
            for kind in ("task.created", "task.linked", "task.handoff", "task.resumed"):
                self.assertTrue(
                    any(
                        e["type"] == kind and e["actor"].get("agent_id") == "agent-1"
                        for e in events
                    )
                )
            with self.assertRaisesRegex(ValueError, "cycle"):
                api.link_checkpoint(state, "parent", "child")

    def test_verification_attempt_correlates_and_conflict_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Task", "Next", name="task")
            import subprocess

            real = subprocess.run

            def run(*args, **kwargs):
                if args[0][0] == "/bin/sh":
                    api.save_checkpoint(state, cwd, "Changed goal", "Next", name="task")
                return real(*args, **kwargs)

            with mock.patch.object(api.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "conflict"):
                    api.run_verification(state, "task", "exit 0", cwd, complete=True)
            events = api.history(state, "task")["events"]
            start = next(e for e in events if e["type"] == "verification.started")
            finish = next(e for e in events if e["type"] == "verification.conflict")
            self.assertEqual(
                start["details"]["attempt_id"], finish["details"]["attempt_id"]
            )
            self.assertFalse(finish["details"]["applied"])
            self.assertEqual("open", api.resolve_checkpoint(state, "task")["status"])

    def test_first_verification_does_not_execute_when_legacy_import_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            marker = root / "marker"
            seed = api.save_checkpoint(root / "seed", root, "Task", "Next", name="task")
            path = state / "checkpoints/task.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(Path(seed["_path"]).read_bytes())
            (path.parent / "broken.json").write_text("{broken")
            with self.assertRaises(ValueError):
                api.run_verification(state, "task", "touch " + str(marker), root)
            self.assertFalse(marker.exists())
            self.assertFalse((state / "events").exists())

    def test_valid_hash_but_invalid_revision_and_attempt_are_rejected(self):
        for problem in ("revision", "attempt", "event-version"):
            with tempfile.TemporaryDirectory() as td:
                state = Path(td) / "state"
                cwd = Path(td)
                api.save_checkpoint(state, cwd, "Task", "Next", name="task")
                api.save_checkpoint(state, cwd, "Changed", "Next", name="task")
                path = sorted((state / "events").glob("*.json"))[-1]
                batch = json.loads(path.read_text())
                if problem == "revision":
                    batch["changes"][0]["before_revision"] = "0" * 64
                elif problem == "event-version":
                    batch["observations"][0]["event_version"] = 99
                else:
                    batch["observations"].append(
                        {
                            "event_version": 1,
                            "type": "verification.finished",
                            "task_id": "task",
                            "time": batch["time"],
                            "actor": {},
                            "details": {
                                "attempt_id": "a" * 32,
                                "applied": True,
                                "duration_ns": 1,
                                "exit_code": 0,
                            },
                        }
                    )
                path.write_bytes(journal.canonical(batch) + b"\n")
                with self.assertRaisesRegex(ValueError, "history"):
                    api.list_checkpoints(state)

    def test_event_permissions_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Task", "Next", name="task")
            event = next((state / "events").glob("*.json"))
            self.assertEqual(0o600, event.stat().st_mode & 0o777)
            self.assertEqual(0o700, event.parent.stat().st_mode & 0o777)
            other = cwd / "other"
            other.write_bytes(event.read_bytes())
            event.unlink()
            event.symlink_to(other)
            with self.assertRaisesRegex(ValueError, "symlink"):
                api.list_checkpoints(state)

    def test_actor_environment_and_first_verification_baseline(self):
        import os

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed = api.save_checkpoint(
                root / "seed", root, "Old task", "Next", name="task"
            )
            state = root / "legacy"
            path = state / "checkpoints/task.json"
            path.parent.mkdir(parents=True)
            raw = json.loads(Path(seed["_path"]).read_text())
            raw.pop("name")
            path.write_text(json.dumps(raw))
            with mock.patch.dict(
                os.environ,
                {
                    "AGENT_CHECKPOINT_AGENT_ID": "builder",
                    "AGENT_CHECKPOINT_SESSION_ID": "session",
                },
            ):
                api.run_verification(state, "task", "exit 0", root, complete=True)
            events = api.history(state, "task")["events"]
            self.assertEqual("task.imported", events[0]["type"])
            start = next(e for e in events if e["type"] == "verification.started")
            self.assertEqual("builder", start["actor"]["agent_id"])
            self.assertEqual("session", start["actor"]["session_id"])
            self.assertEqual(
                "complete", api.resolve_checkpoint(state, "task")["status"]
            )
