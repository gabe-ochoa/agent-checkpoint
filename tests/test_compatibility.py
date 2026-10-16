import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_checkpoint import service as hc

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


class RedactionTests(unittest.TestCase):
    def test_high_confidence_secrets_are_redacted(self):
        samples = [
            "github_pat_" + "a" * 40,
            "sk-ant-" + "a" * 32,
            "Authorization: Bearer " + "a" * 40,
            "OBSIDIAN_KEY=" + "a1" * 32,
            "AKIA" + "A" * 16,
            "AIza" + "A" * 35,
            "Cookie: session=" + "a" * 30,
            "https://user:" + "a" * 20 + "@example.com/path",
        ]
        for sample in samples:
            redacted, matches = hc.redact_secrets(sample)
            self.assertTrue(matches, sample)
            self.assertNotIn(sample[-20:], redacted)

    def test_normal_text_is_unchanged(self):
        text = "Build the parser, run tests, and open a draft PR."
        self.assertEqual((text, []), hc.redact_secrets(text))


class CheckpointTests(unittest.TestCase):
    def test_save_offer_resume_complete(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            cp = hc.save_checkpoint(
                state_dir=state,
                cwd=Path(td),
                goal="Finish the harness",
                next_action="Run tests",
                decisions=["Use deterministic checkpoints"],
                verification=["unit tests pending"],
            )
            offered = hc.offer_checkpoint(state, Path(td))
            self.assertEqual(cp["id"], offered["id"])
            context = hc.checkpoint_context(offered, 2000)
            self.assertIn("Finish the harness", context)
            with self.assertRaises(ValueError):
                hc.complete_checkpoint(state, cp["id"])
            hc.complete_checkpoint(state, cp["id"], "python3 -m unittest", 0)
            self.assertIsNone(hc.offer_checkpoint(state, Path(td)))

    def test_named_checkpoint_updates_in_place_and_resolves(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            first = hc.save_checkpoint(
                state,
                ROOT,
                "Upgrade cert-manager",
                "Stage PR",
                decisions=["Stage the upgrade"],
                name="cert-upgrade",
            )
            self.assertEqual("cert-upgrade", first["id"])
            second = hc.save_checkpoint(
                state,
                ROOT,
                "",
                "Verify certificates",
                decisions=["Pause Flux briefly"],
                name="cert-upgrade",
            )
            self.assertEqual(1, len(hc.list_checkpoints(state)))
            self.assertEqual("Upgrade cert-manager", second["goal"])
            self.assertEqual(2, len(second["decisions"]))
            self.assertEqual(
                "cert-upgrade", hc.resolve_checkpoint(state, "cert-upgrade")["id"]
            )
            with self.assertRaises(ValueError):
                hc.save_checkpoint(state, ROOT, "x", "y", name="Bad Name")
            with self.assertRaises(ValueError):
                hc.save_checkpoint(
                    state, Path(td), "Other repo", "y", name="cert-upgrade"
                )
            hc.complete_checkpoint(state, "cert-upgrade", manual=True)
            self.assertEqual(
                "complete", hc.resolve_checkpoint(state, "cert-upgrade")["status"]
            )

    def test_unnamed_checkpoint_gets_readable_id_and_prefix_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            cp = hc.save_checkpoint(
                state,
                Path(td),
                "<command-name>/fix</command-name> Fix the DNS outage!",
                "go",
            )
            self.assertRegex(cp["id"], r"^fix-fix-the-dns-outage-[0-9a-f]{4}$")
            self.assertEqual(
                cp["id"], hc.resolve_checkpoint(state, cp["id"][:-2])["id"]
            )
            with self.assertRaises(FileNotFoundError):
                hc.resolve_checkpoint(state, "nope")

    def test_legacy_checkpoint_gets_derived_name(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            cp = hc.save_checkpoint(
                state,
                Path(td),
                "Restore example",
                "go",
                checkpoint_id="0123456789abcdef",
            )
            raw = json.loads(Path(cp["_path"]).read_text())
            raw.pop("name")
            Path(cp["_path"]).write_text(json.dumps(raw))
            self.assertEqual(
                "0123456789abcdef",
                hc.resolve_checkpoint(state, "restore-example")["id"],
            )

    def test_supersede_close_and_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            old = hc.save_checkpoint(state, ROOT, "Repair example", "a")
            newer = hc.save_checkpoint(
                state,
                ROOT,
                "Repair example",
                "b",
                name="example-repair",
                supersedes=[old["id"]],
            )
            closed = hc.resolve_checkpoint(state, old["id"])
            self.assertEqual(
                ("closed", "superseded", "example-repair"),
                (closed["status"], closed["close_reason"], closed["superseded_by"]),
            )
            self.assertEqual([old["id"]], newer["supersedes"])
            with self.assertRaises(ValueError):
                hc.close_checkpoint(state, "example-repair", "bogus")
            first = hc.save_checkpoint(state, ROOT, "Ship the UI", "a")
            second = hc.save_checkpoint(state, ROOT, "Ship the UI", "b")
            planned = hc.dedupe_checkpoints(state)
            self.assertEqual(
                [{"close": first["id"], "keep": second["id"], "name": "ship-the-ui"}],
                planned,
            )
            self.assertEqual(
                "open", hc.resolve_checkpoint(state, first["id"])["status"]
            )
            hc.dedupe_checkpoints(state, apply=True)
            self.assertEqual(
                "closed", hc.resolve_checkpoint(state, first["id"])["status"]
            )
            self.assertEqual([], hc.dedupe_checkpoints(state))

    def test_clean_prompt_strips_command_markup(self):
        self.assertEqual(
            "",
            hc.clean_prompt(
                "<command-name>/model</command-name><command-message>model</command-message><command-args></command-args>"
            ),
        )
        self.assertEqual(
            "",
            hc.clean_prompt("<local-command-stdout>Set model</local-command-stdout>"),
        )
        self.assertEqual(
            "/example-task for October",
            hc.clean_prompt(
                "<command-message>example-task</command-message>\n"
                "<command-name>/example-task</command-name><command-args>for  October</command-args>"
            ),
        )
        self.assertEqual(
            "@google-drive Look at the sheet",
            hc.clean_prompt(
                "[@google-drive](plugin://google-drive@openai-curated-remote) Look at the sheet"
            ),
        )
        self.assertEqual(
            "Rewritten, 1857 chars.",
            hc.first_sentence("Rewritten, 1857 chars.  ---  **Board meeting**"),
        )

    def test_capture_skips_builtin_only_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "t.jsonl"
            events = [
                {
                    "type": "user",
                    "sessionId": "s1",
                    "cwd": td,
                    "message": {
                        "role": "user",
                        "content": "<command-name>/model</command-name><command-args></command-args>",
                    },
                },
                {
                    "type": "user",
                    "sessionId": "s1",
                    "cwd": td,
                    "message": {
                        "role": "user",
                        "content": "<local-command-stdout>Set model to Opus</local-command-stdout>",
                    },
                },
            ]
            transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n")
            self.assertIsNone(
                hc.capture_checkpoint(
                    Path(td) / "state", transcript, Path(td), "compact"
                )
            )
            events.append(
                {
                    "type": "user",
                    "sessionId": "s1",
                    "cwd": td,
                    "message": {
                        "role": "user",
                        "content": "<command-name>/fix-dns</command-name><command-args>on b21</command-args>",
                    },
                }
            )
            events.append(
                {
                    "type": "assistant",
                    "sessionId": "s1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "**Done** with step one. Next I will restart the service.",
                            }
                        ],
                    },
                }
            )
            transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n")
            record = hc.capture_checkpoint(
                Path(td) / "state", transcript, Path(td), "compact"
            )
            self.assertEqual("/fix-dns on b21", record["goal"])
            self.assertEqual("Last reply: Done with step one.", record["next_action"])

    def test_offer_matches_worktrees_and_returns_several(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            worktree = Path(td) / "wt"

            def git(*a, cwd=repo):
                return subprocess.run(
                    ["git", *a], cwd=cwd, check=True, capture_output=True
                )

            repo.mkdir()
            git("init", "-q")
            git(
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "init",
            )
            git("worktree", "add", "-q", "-b", "side", str(worktree))
            state = Path(td) / "state"
            hc.save_checkpoint(state, worktree, "Worktree task", "a", name="wt-task")
            hc.save_checkpoint(state, repo, "Main task one", "b", name="main-one")
            hc.save_checkpoint(state, repo, "Main task two", "c", name="main-two")
            hc.save_checkpoint(state, Path(td), "Unrelated", "d", name="elsewhere")
            offers = hc.offer_checkpoints(state, repo, limit=5)
            self.assertEqual(
                {"wt-task", "main-one", "main-two"}, {r["name"] for r in offers}
            )
            self.assertTrue(
                next(r for r in offers if r["name"] == "wt-task")["diverged"]
            )
            self.assertEqual(2, len(hc.offer_checkpoints(state, repo, limit=2)))

    def test_same_repo_matches_normalized_remote(self):
        current = {
            "repo": "/a",
            "git_common_dir": "/a/.git",
            "remote": hc._normalize_remote("git@github.com:example/project.git"),
        }
        record = {
            "repo": "/tmp/clone",
            "git_common_dir": "/tmp/clone/.git",
            "remote": hc._normalize_remote("https://github.com/example/project"),
        }
        self.assertTrue(hc.same_repo(record, current))
        self.assertFalse(hc.same_repo({**record, "remote": "github.com/x/y"}, current))

    def test_verify_resolves_pending_and_flags_stale(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            cp = hc.save_checkpoint(
                state,
                Path(td),
                "Ship",
                "go",
                name="ship",
                verification=["make test", "check dashboard"],
            )
            self.assertEqual(0, hc.stale_checks(cp))
            raw = json.loads(Path(cp["_path"]).read_text())
            raw["created_at"] = "2020-01-01T00:00:00+00:00"
            Path(cp["_path"]).write_text(json.dumps(raw))
            self.assertEqual(2, hc.stale_checks(hc.resolve_checkpoint(state, "ship")))
            record = hc.verify_checkpoint(state, "ship", "make test", 1)
            self.assertEqual(
                ["failed", "pending"], [v["status"] for v in record["verification"]]
            )
            self.assertEqual("open", record["status"])
            record = hc.complete_checkpoint(state, "ship", "make test", 0)
            self.assertEqual(
                ["failed", "unresolved", "passed"],
                [v["status"] for v in record["verification"]],
            )

    def test_blocker_kept_and_history_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            hc.save_checkpoint(
                state, Path(td), "Repair", "a", name="repair", blocker="C23 cable"
            )
            kept = hc.save_checkpoint(state, Path(td), "", "b", name="repair")
            self.assertEqual("C23 cable", kept["blocker"])
            changed = hc.save_checkpoint(
                state, Path(td), "", "c", name="repair", blocker="API credits"
            )
            self.assertEqual(
                ["C23 cable"], [b["blocker"] for b in changed["blocker_history"]]
            )
            cleared = hc.save_checkpoint(
                state, Path(td), "", "d", name="repair", blocker=""
            )
            self.assertEqual("", cleared["blocker"])
            self.assertEqual(
                ["C23 cable", "API credits"],
                [b["blocker"] for b in cleared["blocker_history"]],
            )
            self.assertIn(
                "Earlier blockers (resolved): C23 cable; API credits",
                hc.checkpoint_context(cleared),
            )

    def test_work_log_keeps_context_and_folds_superseded(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            old = hc.save_checkpoint(
                state,
                ROOT,
                "Design commons",
                "a",
                decisions=["Use SQLite WAL"],
                blocker="review board",
            )
            hc.save_checkpoint(
                state,
                ROOT,
                "Design commons v2",
                "b",
                name="commons",
                decisions=["One service per realm"],
                artifacts=["docs/example/design.md"],
            )
            hc.close_checkpoint(state, old["id"], "superseded", superseded_by="commons")
            hc.close_checkpoint(
                state,
                "commons",
                "done-elsewhere",
                note="Package finished",
                artifacts=["docs/example/adrs"],
            )
            hc.save_checkpoint(state, ROOT, "Dropped idea", "c", name="dropped")
            hc.close_checkpoint(state, "dropped", "abandoned")
            hc.save_checkpoint(state, Path(td), "Other repo work", "d", name="other")
            hc.complete_checkpoint(state, "other", manual=True, note="shipped")

            entries = hc.work_log(state, ROOT)
            self.assertEqual(["commons"], [e["name"] for e in entries])
            entry = entries[0]
            self.assertEqual("Package finished", entry["outcome"])
            self.assertEqual(
                ["docs/example/design.md", "docs/example/adrs"], entry["artifacts"]
            )
            self.assertEqual(
                ["One service per realm", "Use SQLite WAL"], entry["decisions"]
            )
            self.assertEqual(["review board"], entry["blockers_seen"])
            self.assertEqual([old["id"]], entry["folded"])

            everything = hc.work_log(state, None, None, hc.FINISHED_REASONS)
            self.assertEqual(
                {"commons", "dropped", "other", old["name"]},
                {e["name"] for e in everything},
            )
            rendered = hc.render_work_log(entries)
            self.assertIn("- Outcome: Package finished", rendered)
            self.assertIn("  - Use SQLite WAL", rendered)
            self.assertIn("Also covers 1 superseded checkpoint(s)", rendered)
            self.assertEqual("No finished work in range.", hc.render_work_log([]))

    def test_log_cli_renders_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            hc.save_checkpoint(state, ROOT, "Ship log", "go", name="ship-log")
            ROOT / "agent-checkpoint"

            def run(*a):
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_checkpoint",
                        "--state-dir",
                        str(state),
                        *a,
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )

            closed = run(
                "close",
                "ship-log",
                "--reason",
                "done-elsewhere",
                "--note",
                "Merged in PR 5",
                "--artifact",
                "https://github.com/x/y/pull/5",
            )
            self.assertEqual(0, closed.returncode, closed.stderr)
            out = run("log", "--cwd", str(ROOT)).stdout
            self.assertIn("ship-log (done-elsewhere)", out)
            self.assertIn("`https://github.com/x/y/pull/5`", out)

    def test_checkpoint_payload_is_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            secret = "github_pat_" + "c" * 40
            cp = hc.save_checkpoint(
                state_dir=Path(td), cwd=Path(td), goal=secret, next_action="rotate it"
            )
            raw = Path(cp["_path"]).read_text()
            self.assertNotIn("github_pat_", raw)

    def test_mixed_claude_transcript_captures_only_human_work(self):
        with tempfile.TemporaryDirectory() as td:
            record = hc.capture_checkpoint(
                Path(td), FIXTURES / "claude-mixed.jsonl", ROOT, "compact"
            )
            self.assertIsNotNone(record)
            self.assertEqual("claude", record["platform"])
            self.assertNotIn("Automated example", record["goal"])

    def test_cli_executes_verification_before_completion(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            checkpoint = hc.save_checkpoint(
                state, ROOT, "Finish the harness", "Run verification"
            )
            ROOT / "agent-checkpoint"
            failed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_checkpoint",
                    "--state-dir",
                    str(state),
                    "complete",
                    checkpoint["id"],
                    "--run",
                    "exit 7",
                    "--cwd",
                    str(ROOT),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(1, failed.returncode)
            self.assertEqual(
                "open", hc.resolve_checkpoint(state, checkpoint["id"])["status"]
            )
            passed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_checkpoint",
                    "--state-dir",
                    str(state),
                    "complete",
                    checkpoint["id"],
                    "--run",
                    "exit 0",
                    "--cwd",
                    str(ROOT),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, passed.returncode, passed.stderr)
            self.assertEqual(
                "complete", hc.resolve_checkpoint(state, checkpoint["id"])["status"]
            )

    def test_cli_checks_run_with_caller_path(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = Path(td) / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "python3"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
            state = Path(td) / "state"
            hc.save_checkpoint(state, ROOT, "Check env", "go", name="check-env")
            ROOT / "agent-checkpoint"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_checkpoint",
                    "--state-dir",
                    str(state),
                    "complete",
                    "check-env",
                    "--run",
                    f'test "$(command -v python3)" = "{fake}"',
                    "--cwd",
                    str(ROOT),
                ],
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            )
            self.assertEqual(0, result.returncode, result.stderr)
