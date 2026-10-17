import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_checkpoint

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_offline_install_is_idempotent_and_verifies_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prefix = root / "lib"
            bin_dir = root / "bin"
            source = root / "source"
            (source / "src").mkdir(parents=True)
            shutil.copytree(
                Path(agent_checkpoint.__file__).parent,
                source / "src/agent_checkpoint",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            args = [
                sys.executable,
                str(ROOT / "scripts/install.py"),
                "--prefix",
                str(prefix),
                "--bin-dir",
                str(bin_dir),
                "--source",
                str(source),
            ]
            first = subprocess.run(args, capture_output=True, text=True, check=True)
            second = subprocess.run(args, capture_output=True, text=True, check=True)
            self.assertEqual(
                json.loads(first.stdout)["content_id"],
                json.loads(second.stdout)["content_id"],
            )
            run = subprocess.run(
                [
                    str(bin_dir / "agent-checkpoint"),
                    "--state-dir",
                    str(root / "state"),
                    "save",
                    "--name",
                    "task",
                    "--goal",
                    "Example",
                    "--next",
                    "Continue",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, run.returncode, run.stderr)
            runtime = Path(json.loads(first.stdout)["runtime"])
            file = runtime / "agent_checkpoint/core.py"
            file.chmod(0o600)
            file.write_text("corrupt")
            failed = subprocess.run(
                [str(bin_dir / "agent-checkpoint"), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(1, failed.returncode)
            self.assertIn("reinstall", failed.stderr)

    def test_failed_preparation_preserves_selected_runtime(self):
        installer = runpy.run_path(str(ROOT / "scripts/install.py"))
        install = installer["install"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            (source / "src").mkdir(parents=True)
            shutil.copytree(
                Path(agent_checkpoint.__file__).parent,
                source / "src/agent_checkpoint",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            prefix, binary = root / "lib", root / "bin"
            first = install(source, prefix, binary)
            selected = (prefix / "current").resolve()
            with (source / "src/agent_checkpoint/__init__.py").open("a") as handle:
                handle.write("\n# new revision\n")
            with mock.patch.dict(
                install.__globals__,
                {"replace_file": mock.Mock(side_effect=OSError("disk full"))},
            ):
                with self.assertRaises(OSError):
                    install(source, prefix, binary)
            self.assertEqual(selected, (prefix / "current").resolve())
            self.assertEqual(selected, Path(first["runtime"]).resolve())
            run = subprocess.run(
                [str(binary / "agent-checkpoint"), "--help"], capture_output=True
            )
            self.assertEqual(0, run.returncode)
