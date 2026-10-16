"""Reference model: invalid operations leave state unchanged."""

import random
import tempfile
import unittest
from pathlib import Path

from agent_checkpoint import service as api


class Oracle:
    def __init__(self):
        self.status = "open"

    def apply(self, operation):
        if operation == "save":
            self.status = "open"
            return True
        if operation in ("resume", "log"):
            return True
        if self.status != "open":
            return False
        if operation == "complete":
            self.status = "complete"
        if operation == "close":
            self.status = "closed"
        return True


class LifecycleTests(unittest.TestCase):
    def test_seeded_sequences(self):
        for seed in range(40):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as td:
                state = Path(td) / "state"
                cwd = Path(td)
                oracle = Oracle()
                api.save_checkpoint(state, cwd, "Example", "Continue", name="task")
                rng = random.Random(seed)
                for _ in range(30):
                    op = rng.choice(
                        ["save", "verify", "complete", "close", "resume", "log"]
                    )
                    valid = oracle.apply(op)

                    def invoke():
                        if op == "save":
                            return api.save_checkpoint(
                                state, cwd, "", "Continue", name="task"
                            )
                        if op == "verify":
                            return api.verify_checkpoint(state, "task", "test", 1)
                        if op == "complete":
                            return api.complete_checkpoint(state, "task", manual=True)
                        if op == "close":
                            return api.close_checkpoint(state, "task", "abandoned")
                        if op == "resume":
                            return api.resolve_checkpoint(state, "task")
                        return api.work_log(state, None, None)

                    if valid:
                        invoke()
                    else:
                        with self.assertRaises(ValueError):
                            invoke()
                    self.assertEqual(
                        oracle.status,
                        api.resolve_checkpoint(state, "task")["status"],
                        (seed, op),
                    )
