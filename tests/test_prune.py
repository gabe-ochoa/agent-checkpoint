"""Oracle for idle-checkpoint pruning.

Auto-captured sessions (claude/codex platform, no decisions, no blocker) go
stale fast; deliberately saved or blocked work carries intent and waits longer.
Pruning closes as "abandoned" with a note, never deletes, never touches the
caller's own session, and defaults to a dry run.
"""
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_checkpoint import service as hc

ROOT = Path(__file__).resolve().parents[1]


def later(days):
    return datetime.now(UTC) + timedelta(days=days)


class PruneOracle(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.state = Path(self.td.name) / "state"

    def tearDown(self):
        self.td.cleanup()

    def save(self, goal, **kw):
        return hc.save_checkpoint(self.state, ROOT, goal, "next", **kw)

    def test_thresholds_by_kind(self):
        auto = self.save("auto session", platform="claude", session_id="s-auto")
        codex = self.save("codex session", platform="codex", session_id="s-codex")
        manual = self.save("manual task", decisions=["picked A"])
        blocked = self.save("blocked task", platform="codex", blocker="waiting on Gabe")

        at20 = {a["id"] for a in hc.prune_checkpoints(self.state, now=later(20))}
        self.assertEqual({auto["id"], codex["id"]}, at20)

        at61 = {a["id"] for a in hc.prune_checkpoints(self.state, now=later(61))}
        self.assertEqual({auto["id"], codex["id"], manual["id"], blocked["id"]}, at61)

        self.assertEqual([], hc.prune_checkpoints(self.state, now=later(5)))

    def test_dry_run_changes_nothing_and_apply_closes_as_abandoned(self):
        auto = self.save("auto session", platform="claude", session_id="s1")
        actions = hc.prune_checkpoints(self.state, now=later(30))
        self.assertEqual("open", hc.resolve_checkpoint(self.state, auto["id"])["status"])
        self.assertEqual("auto-captured", actions[0]["kind"])
        self.assertGreaterEqual(actions[0]["idle_days"], 30)

        hc.prune_checkpoints(self.state, apply=True, now=later(30))
        closed = hc.resolve_checkpoint(self.state, auto["id"])
        self.assertEqual(("closed", "abandoned"), (closed["status"], closed["close_reason"]))
        self.assertIn("pruned after", str(closed.get("outcome")))
        # Idempotent: nothing left to prune.
        self.assertEqual([], hc.prune_checkpoints(self.state, apply=True, now=later(30)))

    def test_keeps_current_session_and_finished_records(self):
        mine = self.save("my session", platform="claude", session_id="current")
        done = self.save("done task", platform="claude", session_id="s2")
        hc.complete_checkpoint(self.state, done["id"], manual=True)
        ids = {a["id"] for a in hc.prune_checkpoints(self.state, now=later(90), keep_session="current")}
        self.assertNotIn(mine["id"], ids)
        self.assertNotIn(done["id"], ids)

    def test_custom_thresholds_and_validation(self):
        auto = self.save("auto session", platform="codex")
        self.assertEqual([auto["id"]], [a["id"] for a in hc.prune_checkpoints(
            self.state, now=later(4), auto_days=3)])
        for bad in ({"auto_days": 0}, {"manual_days": -1}):
            with self.assertRaises(ValueError):
                hc.prune_checkpoints(self.state, **bad)

    def test_cli_dry_run_then_apply(self):
        self.save("auto session", platform="claude")
        env = {"AGENT_CHECKPOINT_STATE": str(self.state), "PYTHONPATH": str(ROOT / "src")}
        run = lambda *a: subprocess.run(
            [sys.executable, "-m", "agent_checkpoint", "--state-dir", str(self.state), "prune",
             "--auto-days", "1", "--manual-days", "1", *a],
            capture_output=True, text=True, env={**env, "PATH": "/usr/bin:/bin"})
        # Freshly saved: nothing idle yet even with 1-day thresholds.
        out = run()
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertIn("no idle open checkpoints", out.stdout)


if __name__ == "__main__":
    unittest.main()
