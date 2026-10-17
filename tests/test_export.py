import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from agent_checkpoint import service as api
from agent_checkpoint import telemetry


class ExportTests(unittest.TestCase):
    def events(self, root):
        state = root / "state"
        with api.actor_context(
            session_id="/private/session", agent_id="sensitive-agent"
        ):
            api.save_checkpoint(
                state, root, "Secret project goal", "Next", name="sensitive-task"
            )
            api.run_verification(state, "sensitive-task", "exit 0", root, complete=True)
        return state

    def test_logs_and_spans_wire_format_and_default_privacy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = self.events(root)
            for signal in ("logs", "traces"):
                result = telemetry.export(state, signal=signal, limit=10000)
                raw = json.dumps(result["payload"])
                for sensitive in (
                    "Secret project goal",
                    "sensitive-task",
                    "sensitive-agent",
                    "/private/session",
                    td,
                    "exit 0",
                ):
                    self.assertNotIn(sensitive, raw)
                self.assertIn("traceId", raw)
                self.assertNotIn("time_unix_nano", raw)
            span = telemetry.export(state, signal="traces")["payload"]["resourceSpans"][
                0
            ]["scopeSpans"][0]["spans"][0]
            self.assertEqual(1, span["kind"])
            self.assertEqual(32, len(span["traceId"]))
            self.assertEqual(16, len(span["spanId"]))
            self.assertIsInstance(span["startTimeUnixNano"], str)
            self.assertEqual(1, span["status"]["code"])
            self.assertIn(
                "Secret project goal",
                json.dumps(telemetry.export(state, include_content=True)["payload"]),
            )

    def test_local_http_receiver_and_partial_success(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            response = {}

            def do_POST(self):
                requests.append(
                    (
                        self.path,
                        self.headers.get("Content-Type"),
                        json.loads(
                            self.rfile.read(int(self.headers["Content-Length"]))
                        ),
                    )
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(self.response).encode())

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                state = self.events(Path(td))
                endpoint = f"http://127.0.0.1:{server.server_port}/v1/logs"
                telemetry.export(state, endpoint=endpoint)
                self.assertEqual("/v1/logs", requests[0][0])
                self.assertEqual("application/json", requests[0][1])
                Handler.response = {
                    "partialSuccess": {
                        "rejectedLogRecords": "1",
                        "errorMessage": "some rejected",
                    }
                }
                before = len(list((state / "events").glob("*.json")))
                with self.assertRaisesRegex(RuntimeError, "partial"):
                    telemetry.export(state, endpoint=endpoint)
                self.assertEqual(2, len(requests))
                self.assertEqual(before, len(list((state / "events").glob("*.json"))))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_transport_policy_and_export_size_bound(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.events(Path(td))
            for endpoint in (
                "http://example.com/v1/logs",
                "https://" + "user:password@" + "example.com/v1/logs",
                "file:///tmp/x",
            ):
                with self.assertRaises(ValueError):
                    telemetry.export(state, endpoint=endpoint)
            with mock.patch.object(telemetry, "MAX_EXPORT_BYTES", 20):
                with self.assertRaisesRegex(ValueError, "narrower"):
                    telemetry.export(state)

    def test_normal_operations_never_export(self):
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(
                telemetry, "send", side_effect=AssertionError("network forbidden")
            ),
        ):
            state = self.events(Path(td))
            api.list_checkpoints(state)
            api.history(state, "sensitive-task")
            api.stats(state)

    def test_redirect_timeout_malformed_and_oversized_responses(self):
        from urllib.error import URLError

        class Response:
            status = 200

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                return self.body[:limit]

        with tempfile.TemporaryDirectory() as td:
            state = self.events(Path(td))
            for response in (b"not-json", b"[]", b"x" * 100):
                opener = mock.Mock()
                opener.open.return_value = Response(response)
                with (
                    mock.patch.object(telemetry, "build_opener", return_value=opener),
                    mock.patch.object(telemetry, "MAX_RESPONSE_BYTES", 50),
                ):
                    with self.assertRaises(RuntimeError):
                        telemetry.export(
                            state, endpoint="http://127.0.0.1:4318/v1/logs"
                        )
            opener = mock.Mock()
            opener.open.side_effect = URLError("sensitive error detail")
            with mock.patch.object(telemetry, "build_opener", return_value=opener):
                with self.assertRaisesRegex(RuntimeError, "transport failed") as error:
                    telemetry.export(
                        state, endpoint="http://127.0.0.1:4318/v1/logs", timeout=0.01
                    )
                self.assertNotIn("sensitive", str(error.exception))
            with self.assertRaisesRegex(RuntimeError, "redirects"):
                telemetry.NoRedirect().redirect_request(
                    None, None, None, None, None, None
                )

    def test_export_identifiers_and_cursors_are_stable(self):
        with tempfile.TemporaryDirectory() as td:
            state = self.events(Path(td))
            first = telemetry.export(state, limit=2)
            second = telemetry.export(state, limit=2)
            self.assertEqual(first, second)
            self.assertTrue(first["has_more"])
            next_page = telemetry.export(state, limit=2, after=first["next_cursor"])
            self.assertNotEqual(first["next_cursor"], next_page["next_cursor"])

    def test_legacy_known_secrets_do_not_reach_opt_in_export(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            seed = api.save_checkpoint(
                root / "seed", root, "Legacy", "Next", name="task"
            )
            state = root / "legacy"
            path = state / "checkpoints/task.json"
            path.parent.mkdir(parents=True)
            secret = "github_pat_" + "x" * 40
            record = json.loads(Path(seed["_path"]).read_text())
            record["goal"] = secret
            record["verification"] = [secret]
            path.write_text(json.dumps(record))
            api.run_verification(
                state, "task", "exit 0 # " + secret, root, complete=True
            )
            for signal in ("logs", "traces"):
                self.assertNotIn(
                    secret,
                    json.dumps(
                        telemetry.export(state, signal=signal, include_content=True)[
                            "payload"
                        ]
                    ),
                )
            self.assertNotIn(
                secret, next((state / "events").glob("*.json")).read_text()
            )
