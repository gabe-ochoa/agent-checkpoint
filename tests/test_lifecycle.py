import json
import tempfile
import unittest
from pathlib import Path

from agent_checkpoint import integration as api


class LifecycleTests(unittest.TestCase):
    def test_journal_recovery_uses_the_same_history_writer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            t = root / "transcript.jsonl"
            t.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "synthetic",
                        "origin": {"kind": "human"},
                        "message": {"content": "Continue the deployment plan"},
                    }
                )
                + "\n"
            )
            api.record_journal(
                state, {"session_id": "synthetic", "transcript_path": str(t)}, root
            )
            self.assertEqual(1, len(list((state / "journals").glob("*.json"))))
            result = api.consolidate_journals(state, root)
            self.assertEqual(1, result["recovered"])
            self.assertEqual([], list((state / "journals").glob("*.json")))
            record = api.offer_checkpoint(state, root)
            self.assertEqual("Continue the deployment plan", record["goal"])
            self.assertTrue(api.history(state, record["id"])["events"])

    def test_missing_transcript_keeps_pointer_for_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / "state"
            api.record_journal(
                state,
                {"session_id": "synthetic", "transcript_path": str(root / "missing")},
                root,
            )
            self.assertEqual(1, api.consolidate_journals(state, root)["failed"])
            self.assertEqual(1, len(list((state / "journals").glob("*.json"))))
