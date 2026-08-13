from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from codex_goal_guardian.config import TargetConfig
from codex_goal_guardian import ownership
from codex_goal_guardian.ownership import (
    _arguments_match_command,
    _command_line_matches,
    _desktop_processes_use_shared_app_server,
    _is_app_server_arguments,
    _is_app_server_command_line,
    cli_process_is_running,
    desktop_uses_shared_app_server,
)


ROOT = Path(__file__).resolve().parents[1]


class OwnershipProbeTests(unittest.TestCase):
    def test_windows_process_probe_is_cached_within_one_pass(self) -> None:
        completed = subprocess.CompletedProcess(
            args=("powershell.exe",),
            returncode=0,
            stdout='[{"ProcessId":1,"CommandLine":"codex"}]',
            stderr="",
        )
        with (
            mock.patch.object(ownership, "_WINDOWS_PROCESS_CACHE", None),
            mock.patch.object(
                ownership.shutil,
                "which",
                return_value="powershell.exe",
            ),
            mock.patch.object(
                ownership.subprocess,
                "run",
                return_value=completed,
            ) as run,
            mock.patch.object(
                ownership.time,
                "monotonic",
                side_effect=(10.0, 12.0),
            ),
        ):
            first = ownership._windows_process_records()
            second = ownership._windows_process_records()

        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)

    def test_windows_process_probe_uses_last_cache_on_timeout(self) -> None:
        cached = [{"ProcessId": 7, "CommandLine": "codex"}]
        with (
            mock.patch.object(
                ownership,
                "_WINDOWS_PROCESS_CACHE",
                (1.0, cached),
            ),
            mock.patch.object(
                ownership.shutil,
                "which",
                return_value="powershell.exe",
            ),
            mock.patch.object(
                ownership.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=("powershell.exe",),
                    timeout=15,
                ),
            ),
            mock.patch.object(
                ownership.time,
                "monotonic",
                return_value=10.0,
            ),
        ):
            records = ownership._windows_process_records()

        self.assertEqual(records, cached)

    def test_app_server_process_is_not_treated_as_cli_owner(self) -> None:
        self.assertTrue(
            _is_app_server_arguments(
                ["/usr/bin/node", "/opt/codex/bin/codex.js", "app-server"]
            )
        )
        self.assertTrue(
            _is_app_server_command_line(
                '"C:/Program Files/nodejs/node.exe" "C:/codex.js" app-server'
            )
        )

    def test_node_entrypoint_survives_different_node_argv_zero(self) -> None:
        command = (
            "/opt/node/bin/node",
            "/home/user/.local/lib/codex/bin/codex.js",
        )

        self.assertTrue(
            _arguments_match_command(
                ("node", command[1], "--model", "gpt-5"),
                command,
            )
        )
        self.assertFalse(
            _arguments_match_command(
                ("/opt/node/bin/node", "/workspace/server.js"),
                command,
            )
        )

    def test_windows_command_line_rejects_unrelated_node_process(self) -> None:
        command = (
            "C:/Program Files/nodejs/node.exe",
            "C:/Users/test/AppData/Roaming/npm/node_modules/"
            "@openai/codex/bin/codex.js",
        )

        self.assertTrue(
            _command_line_matches(
                '"C:/Program Files/nodejs/node.exe" '
                '"C:/Users/test/AppData/Roaming/npm/node_modules/'
                '@openai/codex/bin/codex.js" app-server',
                command,
            )
        )
        self.assertFalse(
            _command_line_matches(
                '"C:/Program Files/nodejs/node.exe" C:/workspace/server.js',
                command,
            )
        )

    def test_bare_codex_command_requires_an_executable_token(self) -> None:
        command = ("codex",)

        self.assertTrue(
            _command_line_matches(
                '"C:/Users/test/AppData/Roaming/npm/codex.cmd" exec',
                command,
            )
        )
        self.assertTrue(
            _command_line_matches(
                '"C:/tools/codex.exe" app-server',
                command,
            )
        )
        self.assertFalse(
            _command_line_matches(
                'python.exe C:/workspace/codex-goal-guardian/lab.py',
                command,
            )
        )
        self.assertFalse(
            _command_line_matches(
                'node.exe C:/tools/codex.js app-server',
                command,
            )
        )

    def test_live_configured_process_is_detected(self) -> None:
        script = ROOT / "tests/fixtures/fake_app_server.py"
        process = subprocess.Popen(
            (sys.executable, str(script)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(_stop_process, process)
        target = TargetConfig(
            name="test",
            command=(sys.executable, str(script)),
            codex_home="/tmp/codex-goal-guardian-test",
        )

        self.assertTrue(cli_process_is_running(target))

    def test_desktop_shared_runtime_requires_no_embedded_child(self) -> None:
        app = {
            "ExecutablePath": (
                "C:\\Program Files\\WindowsApps\\"
                "OpenAI.Codex_1.0_x64__test\\app\\ChatGPT.exe"
            ),
            "CommandLine": "ChatGPT.exe",
        }
        embedded = {
            "ExecutablePath": (
                "C:\\Program Files\\WindowsApps\\"
                "OpenAI.Codex_1.0_x64__test\\app\\resources\\codex.exe"
            ),
            "CommandLine": "codex.exe app-server",
        }

        self.assertTrue(
            _desktop_processes_use_shared_app_server([app])
        )
        self.assertFalse(
            _desktop_processes_use_shared_app_server([app, embedded])
        )
        self.assertFalse(
            _desktop_processes_use_shared_app_server([])
        )

    def test_desktop_shared_runtime_reads_persisted_user_setting(self) -> None:
        shared_url = "ws://127.0.0.1:47831/rpc"
        target = TargetConfig(
            name="desktop",
            command=("codex",),
            codex_home="/tmp/codex-goal-guardian-test",
            recovery_mode="desktop_goal_state",
            app_server_url=shared_url,
        )
        app = {
            "ExecutablePath": (
                "C:\\Program Files\\WindowsApps\\"
                "OpenAI.Codex_1.0_x64__test\\app\\ChatGPT.exe"
            ),
            "CommandLine": "ChatGPT.exe",
        }

        with (
            mock.patch(
                "codex_goal_guardian.ownership.os.name",
                "nt",
            ),
            mock.patch(
                "codex_goal_guardian.ownership."
                "_windows_user_environment_value",
                return_value=shared_url,
            ),
            mock.patch(
                "codex_goal_guardian.ownership._windows_process_records",
                return_value=[app],
            ),
        ):
            self.assertTrue(desktop_uses_shared_app_server(target))

    def test_desktop_shared_runtime_rejects_different_user_setting(self) -> None:
        target = TargetConfig(
            name="desktop",
            command=("codex",),
            codex_home="/tmp/codex-goal-guardian-test",
            recovery_mode="desktop_goal_state",
            app_server_url="ws://127.0.0.1:47831/rpc",
        )

        with (
            mock.patch(
                "codex_goal_guardian.ownership.os.name",
                "nt",
            ),
            mock.patch(
                "codex_goal_guardian.ownership."
                "_windows_user_environment_value",
                return_value="ws://127.0.0.1:47832/rpc",
            ),
        ):
            self.assertFalse(desktop_uses_shared_app_server(target))


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=2)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    unittest.main()
