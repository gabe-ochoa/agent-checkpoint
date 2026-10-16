#!/usr/bin/env python3
"""Install a wheel into a fresh venv and validate every CLI command outside checkout."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import venv
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    root = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert all(
            n.startswith(("agent_checkpoint/", "agent_checkpoint-")) for n in names
        ), names
        metadata = archive.read(
            next(n for n in names if n.endswith("/METADATA"))
        ).decode()
        assert "Requires-Dist:" not in metadata, "unexpected runtime dependency"
        assert any(n.endswith("/LICENSE") for n in names), "missing license"
    with tempfile.TemporaryDirectory(prefix="checkpoint-wheel-") as td:
        work = Path(td)
        env = work / "venv"
        venv.EnvBuilder(with_pip=True).create(env)
        python = env / "bin/python"
        tool = env / "bin/agent-checkpoint"
        clean = os.environ.copy()
        clean.pop("PYTHONPATH", None)
        for key in ("AGENT_CHECKPOINT_STATE", "AGENT_HARNESS_STATE"):
            clean.pop(key, None)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-index", str(wheel)],
            cwd=work,
            env=clean,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        shutil.copytree(
            root / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__")
        )
        subprocess.run(
            [str(python), "-m", "unittest", "discover", "-s", "tests"],
            cwd=work,
            env=clean,
            check=True,
        )
        state = work / "state"

        def cli(*parts, expected=0):
            result = subprocess.run(
                [str(tool), "--state-dir", str(state), *parts],
                cwd=work,
                env=clean,
                capture_output=True,
                text=True,
            )
            assert result.returncode == expected, (
                parts,
                result.returncode,
                result.stderr,
            )
            return result.stdout

        cli(
            "save",
            "--name",
            "example",
            "--goal",
            "Example task",
            "--next",
            "Run a check",
        )
        cli("list")
        json.loads(cli("list", "--json"))
        cli("offer")
        cli("resume", "example", "--context")
        cli("verify", "example", "--run", "exit 7", expected=1)
        cli("complete", "example", "--run", "exit 0", "--note", "Checked")
        cli("log")
        json.loads(cli("log", "--json"))
        cli("save", "--name", "example", "--next", "Reopened")
        cli("close", "example", "--reason", "done-elsewhere")
        cli("dedupe")
        cli("offer", expected=1)
        transcript = work / "synthetic.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "synthetic",
                    "message": {"content": "Example capture"},
                }
            )
            + "\n"
        )
        cli("capture", "--transcript", str(transcript), "--event", "compact")
        cli("--help")
    print(
        json.dumps(
            {
                "wheel": wheel.name,
                "installed_tests": "passed",
                "all_commands": "passed",
                "runtime_dependencies": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
