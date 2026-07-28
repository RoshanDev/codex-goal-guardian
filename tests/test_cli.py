from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

from codex_goal_guardian.cli import (
    _report_log_fingerprint,
    _report_worth_logging,
    build_parser,
    is_windows_shim_under_wsl,
    main,
)


class CliRoutingTests(unittest.TestCase):
    def test_run_once_routes_dry_run_and_json_flags(self) -> None:
        arguments = build_parser().parse_args(
            ["run-once", "--config", "/tmp/config.json", "--dry-run", "--json"]
        )

        self.assertEqual(arguments.command_name, "run-once")
        self.assertEqual(arguments.config, "/tmp/config.json")
        self.assertTrue(arguments.dry_run)
        self.assertTrue(arguments.json_output)

    def test_watch_requires_positive_interval(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["watch", "--config", "/tmp/config.json", "--interval", "0"]
            )

    def test_windows_shim_detection_under_wsl(self) -> None:
        self.assertTrue(
            is_windows_shim_under_wsl(
                "codex",
                "/mnt/c/Users/example/AppData/Roaming/npm/codex",
                is_wsl=True,
            )
        )
        self.assertFalse(
            is_windows_shim_under_wsl(
                "codex",
                "/home/example/.bun/bin/codex",
                is_wsl=True,
            )
        )

    def test_steady_healthy_report_is_not_logged(self) -> None:
        report = {
            "targets": [
                {
                    "status": "healthy",
                    "state_changed": False,
                    "actions": [],
                    "errors": [],
                }
            ]
        }

        self.assertFalse(_report_worth_logging(report))

    def test_log_fingerprint_ignores_probe_timing(self) -> None:
        base = {
            "healthy": True,
            "health_reason": "HTTP 403 reachable",
            "targets": [{"name": "wsl", "status": "healthy"}],
        }
        first = {**base, "timestamp": 100, "health_elapsed_ms": 300}
        second = {**base, "timestamp": 200, "health_elapsed_ms": 900}

        self.assertEqual(
            _report_log_fingerprint(first),
            _report_log_fingerprint(second),
        )

    def test_run_once_invokes_engine_with_dry_run(self) -> None:
        report = {"healthy": True, "targets": []}
        engine = Mock()
        engine.run_once.return_value = report
        output = io.StringIO()

        with (
            patch(
                "codex_goal_guardian.cli.load_config",
                return_value=SimpleNamespace(log_path="/tmp/guardian.jsonl"),
            ),
            patch("codex_goal_guardian.cli.RecoveryEngine", return_value=engine),
            patch("codex_goal_guardian.cli.append_json_log"),
            redirect_stdout(output),
        ):
            exit_code = main(
                [
                    "run-once",
                    "--config",
                    "/tmp/config.json",
                    "--dry-run",
                    "--json",
                ]
            )

        self.assertEqual(exit_code, 0)
        engine.run_once.assert_called_once_with(ANY, dry_run=True)
        self.assertEqual(json.loads(output.getvalue()), report)


class HookRecordTests(unittest.TestCase):
    def test_hook_record_omits_prompt_and_tool_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "hook.jsonl"
            payload = {
                "hook_event_name": "Stop",
                "session_id": "session-1",
                "thread_id": "thread-1",
                "reason": "Reconnecting 5/5",
                "prompt": "private prompt",
                "tool_input": {"secret": "do-not-log"},
            }
            with (
                patch("sys.stdin", io.StringIO(json.dumps(payload))),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = main(["hook-record", "--log", str(log_path), "--json"])

            self.assertEqual(exit_code, 0)
            stored = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["kind"], "hook")
            self.assertEqual(stored["reason"], "Reconnecting 5/5")
            self.assertNotIn("prompt", stored)
            self.assertNotIn("tool_input", stored)


if __name__ == "__main__":
    unittest.main()
