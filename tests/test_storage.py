import json
import multiprocessing as mp
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_checkpoint import core, storage
from agent_checkpoint import service as api


def update_worker(state, cwd, index):
    api.save_checkpoint(
        Path(state),
        Path(cwd),
        "",
        "Continue",
        name="task",
        decisions=[f"decision-{index}"],
    )


def replace_worker(path, ready, phase):
    real = storage.os.replace

    def barrier(a, b):
        if phase == "before":
            ready.set()
            time.sleep(60)
        real(a, b)
        if phase == "after":
            ready.set()
            time.sleep(60)

    storage.os.replace = barrier
    with storage.locked(Path(path).parent):
        storage.atomic_write(Path(path), "new")


class StorageTests(unittest.TestCase):
    def test_multiprocess_updates_preserve_all_decisions(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Task", "Continue", name="task")
            context = mp.get_context("spawn")
            children = [
                context.Process(target=update_worker, args=(str(state), td, i))
                for i in range(12)
            ]
            for child in children:
                child.start()
            for child in children:
                child.join(15)
                self.assertEqual(0, child.exitcode)
            self.assertEqual(
                {f"decision-{i}" for i in range(12)},
                set(api.resolve_checkpoint(state, "task")["decisions"]),
            )

    def test_kill_at_replace_boundaries_and_lock_recovery(self):
        for phase, expected in [("before", "old"), ("after", "new")]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "record"
                path.write_text("old")
                context = mp.get_context("spawn")
                ready = context.Event()
                child = context.Process(
                    target=replace_worker, args=(str(path), ready, phase)
                )
                child.start()
                try:
                    self.assertTrue(ready.wait(10))
                    child.kill()
                    child.join(10)
                    self.assertEqual(expected, path.read_text())
                    with storage.locked(Path(td), timeout=0.1):
                        pass
                finally:
                    if child.is_alive():
                        child.kill()
                        child.join()

    def test_lock_timeout_and_cli_error(self):
        with tempfile.TemporaryDirectory() as td, storage.locked(Path(td)):
            with self.assertRaisesRegex(RuntimeError, "lock timeout"):
                with storage.locked(Path(td), timeout=0.03):
                    pass

    def test_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            record = api.save_checkpoint(state, cwd, "Task", "Continue", name="task")
            self.assertEqual(0o600, Path(record["_path"]).stat().st_mode & 0o777)
            self.assertEqual(0o700, state.stat().st_mode & 0o777)
            victim = Path(td) / "victim"
            victim.write_text("keep")
            Path(record["_path"]).unlink()
            Path(record["_path"]).symlink_to(victim)
            with self.assertRaisesRegex(ValueError, "symlink"):
                api.resolve_checkpoint(state, "task")
            self.assertEqual("keep", victim.read_text())
            Path(record["_path"]).unlink()
            (state / ".lock").unlink()
            (state / ".lock").symlink_to(victim)
            with self.assertRaisesRegex(ValueError, "symlink"):
                api.list_checkpoints(state)

    def test_corruption_is_not_overwritten(self):
        mutations = [
            lambda r: r.update(schema_version=2),
            lambda r: r.update(id="wrong"),
            lambda r: r.update(decisions=[None]),
            lambda r: r.update(verification=[{}]),
            lambda r: r.update(blocker_history=[None]),
            lambda r: r.update(updated_at="yesterday"),
            lambda r: r.update(status="unknown"),
        ]
        for mutate in mutations:
            with tempfile.TemporaryDirectory() as td:
                state = Path(td) / "state"
                api.save_checkpoint(state, Path(td), "Task", "Next", name="task")
                path = state / "checkpoints/task.json"
                record = json.loads(path.read_text())
                mutate(record)
                path.write_text(json.dumps(record))
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "corrupt"):
                    api.save_checkpoint(state, Path(td), "", "Next", name="task")
                self.assertEqual(before, path.read_bytes())

    def test_invalid_references(self):
        with tempfile.TemporaryDirectory() as td:
            for ref in ("../outside", "/tmp/a", "UPPER", "x" * 65, ""):
                with self.assertRaises(ValueError):
                    api.resolve_checkpoint(Path(td), ref)

    def test_legacy_state_reading(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            record = api.save_checkpoint(
                state, state, "Legacy", "Next", checkpoint_id="0123456789abcdef"
            )
            path = Path(record["_path"])
            raw = json.loads(path.read_text())
            raw.pop("name")
            raw["verification"] = ["old pending note"]
            path.write_text(json.dumps(raw))
            before = path.read_bytes()
            self.assertEqual(
                "legacy", api.resolve_checkpoint(state, "0123456789abcdef")["name"]
            )
            self.assertEqual(before, path.read_bytes())

    def test_partial_close_reports_progress(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            for name in ("one", "two"):
                api.save_checkpoint(state, Path(td), name, "Next", name=name)
            real = core.close_checkpoint

            def close(*args, **kwargs):
                if args[1] == "two":
                    raise OSError("injected failure")
                return real(*args, **kwargs)

            with mock.patch.object(core, "close_checkpoint", side_effect=close):
                with self.assertRaisesRegex(RuntimeError, "1 of 2"):
                    api.close_many(state, ["one", "two"], "abandoned")
            self.assertEqual("closed", api.resolve_checkpoint(state, "one")["status"])
            self.assertEqual("open", api.resolve_checkpoint(state, "two")["status"])

    def test_close_preflights_every_reference(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            api.save_checkpoint(state, Path(td), "One", "Next", name="one")
            with self.assertRaises(FileNotFoundError):
                api.close_many(state, ["one", "missing"], "abandoned")
            self.assertEqual("open", api.resolve_checkpoint(state, "one")["status"])

    def test_failed_replace_cleans_temp(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "record"
            path.write_text("old")
            with mock.patch.object(
                storage.os, "replace", side_effect=OSError("injected")
            ):
                with self.assertRaises(OSError):
                    storage.atomic_write(path, "new")
            self.assertEqual("old", path.read_text())
            self.assertEqual([path], list(Path(td).iterdir()))

    def test_cli_lock_timeout_has_recovery_guidance(self):
        with tempfile.TemporaryDirectory() as td:
            code = "from functools import partial; from agent_checkpoint import service, cli; service.locked=partial(service.locked, timeout=0.03); raise SystemExit(cli.main())"
            with storage.locked(Path(td)):
                result = subprocess.run(
                    [sys.executable, "-c", code, "--state-dir", td, "list"],
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(1, result.returncode)
            self.assertIn("lock timeout", result.stderr)
            self.assertIn("retry", result.stderr)

    def test_symlinked_checkpoint_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            state.mkdir()
            outside = Path(td) / "outside"
            outside.mkdir()
            (state / "checkpoints").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                api.resolve_checkpoint(state, "task")

    def test_reopen_clears_finished_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Task", "Next", name="task")
            api.close_checkpoint(state, "task", "abandoned", note="Stopped")
            record = api.save_checkpoint(state, cwd, "", "Restart", name="task")
            self.assertEqual("open", record["status"])
            for key in ("outcome", "close_reason", "closed_at"):
                self.assertNotIn(key, record)

    def test_generated_id_collision_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Existing", "Next", name="same")
            with mock.patch.object(
                core, "_checkpoint_id", side_effect=["same", "fresh"]
            ):
                created = api.save_checkpoint(state, cwd, "New", "Next")
            self.assertEqual("fresh", created["id"])
            self.assertEqual("Existing", api.resolve_checkpoint(state, "same")["goal"])
