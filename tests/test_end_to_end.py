from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_goal_guardian.cli import main
from codex_goal_guardian.state import (
    StateStore,
    default_state,
    enqueue_desktop_recovery_request,
    pending_desktop_recovery_requests,
    transition_health,
    was_recovered,
)


class _HealthyHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(204)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return None


class EndToEndTests(unittest.TestCase):
    def test_cli_recovers_through_fake_app_server_after_outage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_path = root / "trace.jsonl"
            state_path = root / "state.json"
            server_script = (
                Path(__file__).parent / "fixtures" / "fake_app_server.py"
            )
            health_server = ThreadingHTTPServer(
                ("127.0.0.1", 0), _HealthyHandler
            )
            thread = threading.Thread(
                target=health_server.serve_forever, daemon=True
            )
            thread.start()
            self.addCleanup(health_server.server_close)
            self.addCleanup(health_server.shutdown)

            state = default_state()
            transition_health(
                state,
                "integration",
                False,
                1,
                required_failures=1,
                now=int(time.time()) - 1,
            )
            StateStore(state_path).save(state)

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_path": str(state_path),
                        "log_path": str(root / "guardian.jsonl"),
                        "health": {
                            "url": (
                                "http://127.0.0.1:"
                                f"{health_server.server_address[1]}/health"
                            ),
                            "timeout_seconds": 2,
                            "required_consecutive_successes": 1,
                            "required_consecutive_failures": 1,
                        },
                        "targets": [
                            {
                                "name": "integration",
                                "command": [
                                    sys.executable,
                                    str(server_script),
                                ],
                                "codex_home": str(root / "codex-home"),
                                "max_thread_age_seconds": 4_000_000_000,
                                "resume_grace_seconds": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {"FAKE_APP_SERVER_TRACE": str(trace_path)},
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "run-once",
                        "--config",
                        str(config_path),
                        "--json",
                    ]
                )

            report = json.loads(output.getvalue())
            stored = StateStore(state_path).load()
            methods = [
                json.loads(line)["method"]
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            report["targets"][0]["actions"][0]["action"], "turn_started"
        )
        self.assertTrue(
            was_recovered(stored, "integration", 1, "thread-1")
        )
        self.assertIn("thread/resume", methods)
        self.assertIn("turn/start", methods)

    def test_desktop_goal_state_reactivation_never_resumes_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_path = root / "trace.jsonl"
            state_path = root / "state.json"
            server_script = (
                Path(__file__).parent / "fixtures" / "fake_app_server.py"
            )
            health_server = ThreadingHTTPServer(
                ("127.0.0.1", 0), _HealthyHandler
            )
            thread = threading.Thread(
                target=health_server.serve_forever, daemon=True
            )
            thread.start()
            self.addCleanup(health_server.server_close)
            self.addCleanup(health_server.shutdown)

            state = default_state()
            enqueue_desktop_recovery_request(
                state,
                "desktop",
                "thread-1",
                now=int(time.time()) - 1,
            )
            StateStore(state_path).save(state)

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_path": str(state_path),
                        "log_path": str(root / "guardian.jsonl"),
                        "health": {
                            "url": (
                                "http://127.0.0.1:"
                                f"{health_server.server_address[1]}/health"
                            ),
                            "timeout_seconds": 2,
                            "required_consecutive_successes": 1,
                            "required_consecutive_failures": 1,
                        },
                        "targets": [
                            {
                                "name": "desktop",
                                "command": [
                                    sys.executable,
                                    str(server_script),
                                ],
                                "codex_home": str(root / "codex-home"),
                                "recovery_mode": "desktop_goal_state",
                                "allowed_sources": ["vscode"],
                                "max_thread_age_seconds": 4_000_000_000,
                                "resume_grace_seconds": 0,
                                "start_recovery_turn": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "FAKE_APP_SERVER_TRACE": str(trace_path),
                        "FAKE_APP_SERVER_SOURCE": "vscode",
                        "FAKE_APP_SERVER_GOAL_STATUS": "blocked",
                    },
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "run-once",
                        "--config",
                        str(config_path),
                        "--json",
                    ]
                )

            report = json.loads(output.getvalue())
            stored = StateStore(state_path).load()
            messages = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]
            methods = [message["method"] for message in messages]

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            report["targets"][0]["actions"][0]["action"],
            "goal_state_reactivated",
        )
        self.assertFalse(
            pending_desktop_recovery_requests(stored, "desktop")
        )
        self.assertIn("thread/goal/set", methods)
        self.assertNotIn("thread/list", methods)
        self.assertNotIn("thread/resume", methods)
        self.assertNotIn("turn/start", methods)
        goal_set = next(
            message
            for message in messages
            if message["method"] == "thread/goal/set"
        )
        self.assertEqual(
            goal_set["params"],
            {"threadId": "thread-1", "status": "active"},
        )


if __name__ == "__main__":
    unittest.main()
