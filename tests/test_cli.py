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
    running_under_wsl,
)
from codex_goal_guardian.config import (
    DEFAULT_RECOVERY_PROMPT,
    GuardianConfig,
    HealthConfig,
    TargetConfig,
)
from codex_goal_guardian.state import (
    StateStore,
    pending_desktop_recovery_requests,
    singleton_supervisor,
)


class CliRoutingTests(unittest.TestCase):
    @patch("codex_goal_guardian.cli.os.name", "nt")
    @patch.dict("codex_goal_guardian.cli.os.environ", {"WSL_DISTRO_NAME": "Ubuntu"})
    def test_windows_process_is_not_misclassified_by_inherited_wsl_env(self) -> None:
        self.assertFalse(running_under_wsl())

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
                return_value=SimpleNamespace(
                    log_path="/tmp/guardian.jsonl",
                    state_path="/tmp/guardian-test-state.json",
                ),
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

    def test_run_once_skips_when_another_supervisor_holds_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = str(Path(temporary) / "state.json")
            config = SimpleNamespace(
                log_path=str(Path(temporary) / "guardian.jsonl"),
                state_path=state_path,
            )
            output = io.StringIO()
            with singleton_supervisor(state_path) as acquired:
                self.assertTrue(acquired)
                with (
                    patch("codex_goal_guardian.cli.load_config", return_value=config),
                    patch("codex_goal_guardian.cli.RecoveryEngine") as engine,
                    redirect_stdout(output),
                ):
                    exit_code = main(
                        ["run-once", "--config", "/tmp/config.json", "--json"]
                    )

            self.assertEqual(exit_code, 0)
            engine.assert_not_called()
            self.assertEqual(
                json.loads(output.getvalue())["status"],
                "supervisor_already_active",
            )

    def test_desktop_request_is_queued_and_duplicate_is_coalesced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = GuardianConfig(
                state_path=str(root / "state.json"),
                log_path=str(root / "guardian.jsonl"),
                health=HealthConfig(),
                targets=(
                    TargetConfig(
                        name="desktop",
                        command=("codex",),
                        codex_home=str(root / "codex-home"),
                        recovery_mode="desktop_goal_state",
                        allowed_sources=("vscode",),
                        start_recovery_turn=False,
                    ),
                ),
                recovery_prompt=DEFAULT_RECOVERY_PROMPT,
            )
            first_output = io.StringIO()
            second_output = io.StringIO()
            with (
                patch(
                    "codex_goal_guardian.cli.load_config",
                    return_value=config,
                ),
                patch("codex_goal_guardian.cli.append_json_log"),
                redirect_stdout(first_output),
            ):
                first_exit = main(
                    [
                        "request-desktop-recovery",
                        "--config",
                        "/tmp/config.json",
                        "--thread-id",
                        "thread-1",
                        "--json",
                    ]
                )
            with (
                patch(
                    "codex_goal_guardian.cli.load_config",
                    return_value=config,
                ),
                patch("codex_goal_guardian.cli.append_json_log"),
                redirect_stdout(second_output),
            ):
                second_exit = main(
                    [
                        "request-desktop-recovery",
                        "--config",
                        "/tmp/config.json",
                        "--thread-id",
                        "thread-1",
                        "--json",
                    ]
                )

            requests = pending_desktop_recovery_requests(
                StateStore(config.state_path).load(),
                "desktop",
            )

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertFalse(json.loads(first_output.getvalue())["coalesced"])
        self.assertTrue(json.loads(second_output.getvalue())["coalesced"])
        self.assertEqual(tuple(requests), ("thread-1",))


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
