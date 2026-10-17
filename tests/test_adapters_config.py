import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_checkpoint import core, transcripts
from agent_checkpoint import service as api


class AdapterConfigTests(unittest.TestCase):
    def test_state_precedence_and_checkpoint_only_config(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".agent").mkdir()
            (root / ".agent/harness.toml").write_text(
                "[checkpoint]\nresume_context_tokens=1234\nmax_offer_age_days=7\n[autonomy]\nmerges=true\n"
            )
            env = {
                "AGENT_CHECKPOINT_STATE": str(root / "new"),
                "AGENT_HARNESS_STATE": str(root / "old"),
                "XDG_STATE_HOME": str(root / "xdg"),
            }
            with mock.patch.dict(os.environ, env):
                state, policy = api.configuration(str(root / "explicit"), root)
                self.assertEqual(root / "explicit", state)
                self.assertEqual(
                    {
                        "checkpoint": {
                            "resume_context_tokens": 1234,
                            "max_offer_age_days": 7,
                        }
                    },
                    policy,
                )
                self.assertEqual(root / "new", api.configuration(None, root)[0])
                del os.environ["AGENT_CHECKPOINT_STATE"]
                self.assertEqual(root / "old", api.configuration(None, root)[0])
                del os.environ["AGENT_HARNESS_STATE"]
                self.assertEqual(
                    root / "xdg/agent-checkpoint", api.configuration(None, root)[0]
                )

    def test_invalid_config_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".agent").mkdir()
            (root / ".agent/harness.toml").write_text(
                '[checkpoint]\nresume_context_tokens="many"'
            )
            with self.assertRaisesRegex(ValueError, "integer"):
                api.configuration(None, root)

    def test_codex_capture_never_completes_and_marks_context_untrusted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t = root / "session.jsonl"
            events = [
                {
                    "type": "session_meta",
                    "payload": {"id": "synthetic", "source": "cli"},
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Build an example"},
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": "Tests passed. Completed successfully.",
                    },
                },
            ]
            t.write_text("\n".join(map(json.dumps, events)))
            record = api.capture_checkpoint(root / "state", t, root, "session-end")
            self.assertEqual("open", record["status"])
            self.assertEqual([], record["verification"])
            self.assertIn("untrusted captured context", api.checkpoint_context(record))
            api.complete_checkpoint(root / "state", record["id"], manual=True)
            self.assertEqual(
                "complete",
                api.capture_checkpoint(root / "state", t, root, "compact")["status"],
            )

    def test_automation_subagent_exclusions_and_custom_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t = root / "t.jsonl"
            for source in ("exec", {"subagent": {}}):
                t.write_text(
                    "\n".join(
                        map(
                            json.dumps,
                            [
                                {"type": "session_meta", "payload": {"source": source}},
                                {
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "user_message",
                                        "message": "Automated example",
                                    },
                                },
                            ],
                        )
                    )
                )
                self.assertIsNone(
                    api.capture_checkpoint(root / "state", t, root, "compact")
                )
            t.write_text(
                json.dumps(
                    {"type": "user", "message": {"content": "Scheduled example"}}
                )
            )
            self.assertIsNone(
                api.capture_checkpoint(
                    root / "state", t, root, "compact", ("scheduled",)
                )
            )

    def test_transcript_limits_and_malformed_event(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t = root / "t.jsonl"
            t.write_text("x" * 100)
            with mock.patch.object(transcripts, "MAX_EVENT_BYTES", 50):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    api.capture_checkpoint(root / "state", t, root, "compact")
            t.write_text("{broken")
            with self.assertRaisesRegex(ValueError, "line 1"):
                api.capture_checkpoint(root / "state", t, root, "compact")

    def test_large_transcript_bounded_retention(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t = root / "t.jsonl"
            with t.open("w") as handle:
                for i in range(10000):
                    handle.write(
                        json.dumps(
                            {"type": "user", "message": {"content": f"Example {i}"}}
                        )
                        + "\n"
                    )
            summary = transcripts.read_summary(t)
            self.assertEqual("Example 0", summary["goal"])
            self.assertEqual("Example 9999", summary["last"])
            self.assertLess(len(json.dumps(summary)), 3000)

    def test_metadata_redaction_and_credential_origin(self):
        secret = "github_pat_" + "a" * 40
        self.assertEqual(
            "example.com/project",
            api._normalize_remote("https://user:short@example.com/project.git"),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = api.save_checkpoint(
                root / "state",
                root,
                "Task",
                "Next",
                name="task",
                artifacts=[secret],
                decisions=[secret],
            )
            self.assertNotIn(secret, Path(record["_path"]).read_text())

    def test_cli_corrupt_record_error(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            (state / "checkpoints").mkdir()
            (state / "checkpoints/task.json").write_text("{bad")
            run = subprocess.run(
                [sys.executable, "-m", "agent_checkpoint", "--state-dir", td, "list"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, run.returncode)
            self.assertIn("restore a backup", run.stderr)
            self.assertNotIn("Traceback", run.stderr)

    def test_large_log_chain_cycles_and_missing_links(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = []
            for i in range(600):
                records.append(
                    dict(
                        schema_version=1,
                        id=f"task-{i}",
                        name=f"task-{i}",
                        status="closed",
                        repo=str(root),
                        goal="Task",
                        next_action="Next",
                        created_at="2026-01-01T00:00:00+00:00",
                        updated_at="2026-01-01T00:00:00+00:00",
                        close_reason="superseded",
                        superseded_by=f"task-{i + 1}",
                        decisions=[f"Decision {i}"],
                    )
                )
            records[-1].update(
                status="complete",
                close_reason="",
                superseded_by="",
                supersedes=["task-0", "missing"],
            )
            records[100]["supersedes"] = ["task-200"]
            with mock.patch.object(core, "list_checkpoints", return_value=records):
                entries = api.work_log(root / "state", None, None)
            self.assertEqual(1, len(entries))
            self.assertEqual(599, len(entries[0]["folded"]))

    def test_legacy_capture_output_redacts_known_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcript = root / "session.jsonl"
            state = root / "state"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": "synthetic",
                        "message": {"content": "Example"},
                    }
                )
            )
            record = api.capture_checkpoint(state, transcript, root, "compact")
            path = Path(record["_path"])
            raw = json.loads(path.read_text())
            raw["goal"] = "github_pat_" + "a" * 40
            raw["status"] = "complete"
            state = root / "legacy"
            path = state / "checkpoints" / path.name
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(raw))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_checkpoint",
                    "--state-dir",
                    str(state),
                    "capture",
                    "--transcript",
                    str(transcript),
                    "--cwd",
                    str(root),
                    "--event",
                    "compact",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("github_pat_", result.stdout)
            self.assertEqual("complete", json.loads(result.stdout)["status"])
