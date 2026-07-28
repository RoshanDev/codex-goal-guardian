import json
from pathlib import Path
import unittest

from codex_goal_guardian import __version__


ROOT = Path(__file__).resolve().parents[1]


class PluginPackagingTests(unittest.TestCase):
    def test_marketplace_points_to_focused_plugin_directory(self) -> None:
        marketplace = json.loads(
            (ROOT / ".claude-plugin/marketplace.json").read_text(
                encoding="utf-8"
            )
        )
        source = marketplace["plugins"][0]["source"]
        plugin_root = (ROOT / source).resolve()

        self.assertEqual(plugin_root.name, "codex-goal-guardian")
        self.assertTrue((plugin_root / ".codex-plugin/plugin.json").is_file())
        self.assertTrue((plugin_root / "hooks/hooks.json").is_file())
        self.assertTrue(
            (plugin_root / "skills/codex-goal-guardian/SKILL.md").is_file()
        )
        self.assertFalse((plugin_root / ".git").exists())
        self.assertFalse((plugin_root / "tests").exists())
        self.assertFalse((plugin_root / "src").exists())

        manifest = json.loads(
            (plugin_root / ".codex-plugin/plugin.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(marketplace["metadata"]["version"], __version__)
        self.assertEqual(marketplace["plugins"][0]["version"], __version__)
        self.assertEqual(manifest["version"], __version__)


if __name__ == "__main__":
    unittest.main()
