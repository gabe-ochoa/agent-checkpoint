import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_checkpoint import service as api


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        api.save_checkpoint(self.state, self.cwd, "Task", "Next", name="task")

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_command_leaves_open(self):
        record, code = api.run_verification(
            self.state, "task", "exit 7", self.cwd, complete=True
        )
        self.assertEqual(7, code)
        self.assertEqual("open", record["status"])
        self.assertEqual("failed", record["verification"][-1]["status"])

    def test_wrong_cwd_and_finished_task_fail_before_execution(self):
        marker = self.root / "marker"
        command = "touch " + shlex.quote(str(marker))
        with self.assertRaisesRegex(ValueError, "cwd"):
            api.run_verification(self.state, "task", command, self.root)
        api.complete_checkpoint(self.state, "task", manual=True)
        with self.assertRaises(ValueError):
            api.run_verification(self.state, "task", command, self.cwd)
        self.assertFalse(marker.exists())

    def test_concurrent_record_change_rejects_completion(self):
        real = subprocess.run

        def run(*args, **kwargs):
            if args[0][0] == "/bin/sh":
                api.save_checkpoint(self.state, self.cwd, "", "New work", name="task")
            return real(*args, **kwargs)

        with mock.patch.object(api.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "conflict"):
                api.run_verification(
                    self.state, "task", "exit 0", self.cwd, complete=True
                )
        self.assertEqual(
            "New work", api.resolve_checkpoint(self.state, "task")["next_action"]
        )
        self.assertEqual([], api.resolve_checkpoint(self.state, "task")["verification"])

    def test_head_change_rejects_completion(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.cwd), *args], check=True, capture_output=True
            )

        git("init", "-q")
        git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "Initial",
        )
        cmd = "git -c user.name=Example -c user.email=example@example.com commit --allow-empty -qm Changed"
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            api.run_verification(self.state, "task", cmd, self.cwd, complete=True)
        self.assertEqual("open", api.resolve_checkpoint(self.state, "task")["status"])

    def test_uncommitted_edits_are_outside_guarantee(self):
        record, code = api.run_verification(
            self.state, "task", "echo changed > example.txt", self.cwd, complete=True
        )
        self.assertEqual(0, code)
        self.assertEqual("complete", record["status"])

    def test_cli_stdout_remains_json(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_checkpoint",
                "--state-dir",
                str(self.state),
                "verify",
                "task",
                "--cwd",
                str(self.cwd),
                "--run",
                "echo diagnostic",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        json.loads(result.stdout)
        self.assertIn("diagnostic", result.stderr)

    def test_cli_conflict_is_actionable(self):
        cmd = (
            "PYTHONPATH="
            + shlex.quote(str(Path(api.__file__).resolve().parents[1]))
            + " "
            + " ".join(
                map(
                    shlex.quote,
                    [
                        sys.executable,
                        "-m",
                        "agent_checkpoint",
                        "--state-dir",
                        str(self.state),
                        "save",
                        "--name",
                        "task",
                        "--next",
                        "Changed",
                        "--cwd",
                        str(self.cwd),
                    ],
                )
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_checkpoint",
                "--state-dir",
                str(self.state),
                "complete",
                "task",
                "--cwd",
                str(self.cwd),
                "--run",
                cmd,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("verification conflict", result.stderr)
