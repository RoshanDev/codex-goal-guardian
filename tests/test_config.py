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
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            config = load_config(path)

        self.assertEqual(config.targets[0].allowed_sources, ("cli",))

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
                    "recovery_mode": "desktop_goal_state",
                    "allowed_sources": ["vscode"],
                    "start_recovery_turn": False,
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
