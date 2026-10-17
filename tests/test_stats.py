import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_checkpoint import journal
from agent_checkpoint import service as api


class StatsTests(unittest.TestCase):
    def test_outcomes_coverage_and_blocked_time(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            with mock.patch.object(
                journal, "utcnow", return_value="2026-01-01T00:00:00+00:00"
            ):
                api.save_checkpoint(
                    state, cwd, "Task", "Next", name="task", blocker="Waiting"
                )
            with mock.patch.object(
                journal, "utcnow", return_value="2026-01-01T00:00:10+00:00"
            ):
                api.save_checkpoint(state, cwd, "Task", "Next", name="task", blocker="")
            api.run_verification(state, "task", "exit 1", cwd)
            api.run_verification(state, "task", "exit 0", cwd, complete=True)
            report = api.stats(state, since_days=0)
            self.assertEqual(10, report["blocked_wall_seconds"])
            self.assertEqual(1, report["counts"]["verification.failed"])
            self.assertEqual(1, report["counts"]["completion.verified"])
            self.assertEqual(0, report["coverage"]["incomplete_verification_attempts"])
            self.assertGreater(report["coverage"]["events_without_session"], 0)
            self.assertGreater(report["verification_run_seconds"], 0)
            api.save_checkpoint(state, cwd, "Task", "Reopened", name="task")
            self.assertEqual(
                1, api.stats(state, since_days=0)["counts"]["task.reopened"]
            )

    def test_baseline_blocked_duration_is_a_lower_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = api.save_checkpoint(
                root / "seed", root, "Task", "Next", name="task", blocker="Waiting"
            )
            state = root / "legacy"
            path = state / "checkpoints/task.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(Path(record["_path"]).read_bytes())
            api.resume_checkpoint(state, "task")
            report = api.stats(state, since_days=0)
            self.assertEqual(["task"], report["blocked_duration_lower_bound_tasks"])
            self.assertEqual("unavailable", report["coverage"]["pre_import_history"])

    def test_missing_finish_and_clock_reversal_are_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            with mock.patch.object(
                journal, "utcnow", return_value="2026-01-01T00:00:10+00:00"
            ):
                api.save_checkpoint(
                    state, root, "Task", "Next", name="task", blocker="Waiting"
                )
            with mock.patch.object(
                journal, "utcnow", return_value="2026-01-01T00:00:00+00:00"
            ):
                api.save_checkpoint(
                    state, root, "Task", "Next", name="task", blocker=""
                )
            with journal.transaction(state, "verification.start") as tx:
                tx.observe(
                    "verification.started",
                    "task",
                    {
                        "attempt_id": "a" * 32,
                        "record_revision": journal.record_revision(tx.records["task"]),
                        "head": "",
                        "command": "example",
                    },
                )
            report = api.stats(state, since_days=0)
            self.assertEqual(1, report["coverage"]["incomplete_verification_attempts"])
            self.assertEqual("clock-reversal", report["clock_anomalies"][0]["kind"])

    def test_filtered_reports_keep_baseline_and_attempt_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed = api.save_checkpoint(
                root / "seed", root, "Old goal", "Next", name="task"
            )
            state = root / "legacy"
            path = state / "checkpoints/task.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(Path(seed["_path"]).read_bytes())
            with (
                mock.patch.object(
                    journal, "utcnow", return_value="2020-01-01T00:00:00+00:00"
                ),
                api.actor_context(agent_id="starter"),
            ):
                with journal.transaction(state, "verification.start") as tx:
                    tx.observe(
                        "verification.started",
                        "task",
                        {
                            "attempt_id": "a" * 32,
                            "record_revision": journal.record_revision(
                                tx.records["task"]
                            ),
                            "head": "",
                            "command": "example",
                        },
                    )
            api.save_checkpoint(state, root, "New goal", "Next", name="task")
            recent = api.history(state, "task", since_days=1)
            self.assertEqual(1, recent["coverage"]["imported_baselines"])
            self.assertEqual(1, recent["coverage"]["incomplete_verification_attempts"])
            self.assertEqual(
                "unavailable",
                api.stats(state, since_days=1)["coverage"]["pre_import_history"],
            )
            with (
                api.actor_context(agent_id="finisher"),
                journal.transaction(state, "verification.finish") as tx,
            ):
                tx.observe(
                    "verification.conflict",
                    "task",
                    {
                        "attempt_id": "a" * 32,
                        "applied": False,
                        "duration_ns": 1,
                        "exit_code": 0,
                    },
                )
            starter = api.history(state, "task", agent_id="starter")
            self.assertEqual(1, starter["coverage"]["imported_baselines"])
            self.assertEqual(0, starter["coverage"]["incomplete_verification_attempts"])
            self.assertEqual(1, starter["coverage"]["filtered_event_count"])
