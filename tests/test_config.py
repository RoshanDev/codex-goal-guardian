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


if __name__ == "__main__":
    unittest.main()
