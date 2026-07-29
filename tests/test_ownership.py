from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from codex_goal_guardian.config import TargetConfig
from codex_goal_guardian.ownership import (
    _arguments_match_command,
    _command_line_matches,
    _desktop_processes_use_shared_app_server,
    cli_process_is_running,
)


ROOT = Path(__file__).resolve().parents[1]


class OwnershipProbeTests(unittest.TestCase):
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
