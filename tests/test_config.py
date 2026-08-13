import json
import tempfile
import unittest
from pathlib import Path

from codex_goal_guardian.config import load_config


class ConfigTests(unittest.TestCase):
    def test_loads_windows_powershell_utf8_bom(self) -> None:
        payload = {
            "schema_version": 1,
            "state_path": "state.json",
            "log_path": "guardian.jsonl",
            "targets": [
                {
                    "name": "windows",
                    "command": ["codex.cmd"],
                    "codex_home": "codex-home",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(
                json.dumps(payload), encoding="utf-8-sig"
            )

            config = load_config(path)

        self.assertEqual(config.targets[0].name, "windows")
        self.assertEqual(config.targets[0].allowed_sources, ("cli", "exec"))
        self.assertEqual(config.targets[0].recovery_mode, "cli_turn")

    def test_normalizes_explicit_allowed_sources(self) -> None:
        payload = {
            "schema_version": 1,
            "state_path": "state.json",
            "log_path": "guardian.jsonl",
            "targets": [
                {
                    "name": "wsl",
                    "command": ["codex"],
                    "codex_home": "codex-home",
                    "allowed_sources": ["CLI"],
                    "model_capacity_retry_limit": 10,
                    "model_capacity_backoff_initial_seconds": 15,
                    "model_capacity_backoff_max_seconds": 600,
                    "model_capacity_fallback_models": ["gpt-fallback"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            config = load_config(path)

        self.assertEqual(config.targets[0].allowed_sources, ("cli",))
        self.assertEqual(config.targets[0].model_capacity_retry_limit, 10)
        self.assertEqual(
            config.targets[0].model_capacity_backoff_initial_seconds, 15
        )
        self.assertEqual(
            config.targets[0].model_capacity_backoff_max_seconds, 600
        )
        self.assertEqual(
            config.targets[0].model_capacity_fallback_models,
            ("gpt-fallback",),
        )

    def test_rejects_invalid_model_capacity_retry_policy(self) -> None:
        payload = {
            "schema_version": 1,
            "state_path": "state.json",
            "log_path": "guardian.jsonl",
            "targets": [
                {
                    "name": "wsl",
                    "command": ["codex"],
                    "codex_home": "codex-home",
                    "model_capacity_retry_limit": 0,
                    "model_capacity_fallback_models": ["gpt-fallback"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "model_capacity_retry_limit"
            ):
                load_config(path)

    def test_loads_desktop_goal_state_mode(self) -> None:
        payload = {
            "schema_version": 1,
            "state_path": "state.json",
            "log_path": "guardian.jsonl",
            "targets": [
                {
                    "name": "windows-desktop-goal-state",
                    "command": ["codex.cmd"],
                    "codex_home": "codex-home",
                    "app_server_url": "ws://127.0.0.1:47831/rpc",
                    "recovery_mode": "desktop_goal_state",
                    "allowed_sources": ["vscode"],
                    "start_recovery_turn": False,
                    "desktop_thread_ids": [" thread-1 "],
                    "prompt_policy_retry_enabled": True,
                    "delegated_continuity_enabled": True,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            config = load_config(path)

        self.assertEqual(
            config.targets[0].recovery_mode,
            "desktop_goal_state",
        )
        self.assertEqual(config.targets[0].allowed_sources, ("vscode",))
        self.assertEqual(
            config.targets[0].app_server_url,
            "ws://127.0.0.1:47831/rpc",
        )
        self.assertEqual(config.targets[0].desktop_thread_ids, ("thread-1",))
        self.assertTrue(config.targets[0].prompt_policy_retry_enabled)
        self.assertTrue(config.targets[0].delegated_continuity_enabled)

    def test_rejects_duplicate_desktop_thread_ids(self) -> None:
        payload = {
            "schema_version": 1,
            "state_path": "state.json",
            "log_path": "guardian.jsonl",
            "targets": [
                {
                    "name": "windows-desktop-goal-state",
                    "command": ["codex.cmd"],
                    "codex_home": "codex-home",
                    "app_server_url": "ws://127.0.0.1:47831/rpc",
                    "recovery_mode": "desktop_goal_state",
                    "desktop_thread_ids": ["thread-1", "thread-1"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "desktop_thread_ids"):
                load_config(path)

    def test_rejects_desktop_mode_without_shared_app_server(self) -> None:
        payload = {
            "schema_version": 1,
            "state_path": "state.json",
            "log_path": "guardian.jsonl",
            "targets": [
                {
                    "name": "windows-desktop-goal-state",
                    "command": ["codex.cmd"],
                    "codex_home": "codex-home",
                    "recovery_mode": "desktop_goal_state",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "app_server_url"):
                load_config(path)

    def test_rejects_unknown_recovery_mode(self) -> None:
        payload = {
            "schema_version": 1,
            "state_path": "state.json",
            "log_path": "guardian.jsonl",
            "targets": [
                {
                    "name": "unsafe",
                    "command": ["codex"],
                    "codex_home": "codex-home",
                    "recovery_mode": "take_over_everything",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "recovery_mode"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
