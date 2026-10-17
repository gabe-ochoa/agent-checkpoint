import multiprocessing as mp
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_checkpoint import service as api
from agent_checkpoint import storage


def writer(state, cwd, ready, phase):
    real = storage.os.replace

    def replace(a, b):
        is_event = Path(b).parent.name == "events"
        if is_event and phase == "before":
            ready.set()
            time.sleep(60)
        real(a, b)
        if is_event and phase == "after":
            ready.set()
            time.sleep(60)

    storage.os.replace = replace
    api.save_checkpoint(Path(state), Path(cwd), "New goal", "Next", name="task")


class JournalFaultTests(unittest.TestCase):
    def test_kill_before_and_after_commit_replays_correct_state(self):
        for phase, goal in (("before", "Old goal"), ("after", "New goal")):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as td:
                state = Path(td) / "state"
                cwd = Path(td)
                api.save_checkpoint(state, cwd, "Old goal", "Next", name="task")
                context = mp.get_context("spawn")
                ready = context.Event()
                child = context.Process(
                    target=writer, args=(str(state), td, ready, phase)
                )
                child.start()
                try:
                    self.assertTrue(ready.wait(10))
                    child.kill()
                    child.join(10)
                    self.assertEqual(
                        goal, api.resolve_checkpoint(state, "task")["goal"]
                    )
                    api.save_checkpoint(state, cwd, "Next goal", "Next", name="task")
                finally:
                    if child.is_alive():
                        child.kill()
                        child.join()

    def test_directory_fsync_failure_reports_uncertain_batch(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Old", "Next", name="task")
            real = storage.os.fsync

            def fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("injected directory failure")
                return real(fd)

            with mock.patch.object(storage.os, "fsync", side_effect=fsync):
                with self.assertRaisesRegex(RuntimeError, "sequence 2, batch"):
                    api.save_checkpoint(state, cwd, "New", "Next", name="task")
            self.assertEqual("New", api.resolve_checkpoint(state, "task")["goal"])

    def test_failure_before_rename_preserves_old_goal(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            api.save_checkpoint(state, cwd, "Old", "Next", name="task")
            with mock.patch.object(
                storage.os, "replace", side_effect=OSError("injected")
            ):
                with self.assertRaises(OSError):
                    api.save_checkpoint(state, cwd, "New", "Next", name="task")
            self.assertEqual("Old", api.resolve_checkpoint(state, "task")["goal"])

    def test_first_commit_flushes_the_journal_parent_before_publish(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            cwd = Path(td)
            order = []
            real_sync = storage.fsync_directory
            real_replace = storage.os.replace

            def sync(path):
                order.append(("sync", Path(path)))
                return real_sync(path)

            def replace(a, b):
                order.append(("replace", Path(b)))
                return real_replace(a, b)

            with (
                mock.patch.object(storage, "fsync_directory", side_effect=sync),
                mock.patch.object(storage.os, "replace", side_effect=replace),
            ):
                api.save_checkpoint(state, cwd, "Task", "Next", name="task")
            publish = next(
                i
                for i, item in enumerate(order)
                if item[0] == "replace" and item[1].parent == state / "events"
            )
            self.assertTrue(
                any(
                    action == "sync" and path == state
                    for action, path in order[:publish]
                )
            )
