from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_launcher() -> ModuleType:
    launcher_path = ROOT / "scripts/guardian-launch.py"
    spec = spec_from_file_location("guardian_launch", launcher_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuardianLauncherTests(unittest.TestCase):
    def test_hidden_child_uses_create_no_window_and_waits_for_exit(self) -> None:
        launcher = load_launcher()
        completed = unittest.mock.Mock(returncode=7)

        with (
            patch.object(launcher.sys, "platform", "win32"),
            patch.object(launcher.subprocess, "run", return_value=completed) as run,
        ):
            result = launcher._run_windows_hidden_child(
                ["wsl.exe", "--exec", "guardian"]
            )

        self.assertEqual(result, 7)
        run.assert_called_once_with(
            ["wsl.exe", "--exec", "guardian"],
            stdin=launcher.subprocess.DEVNULL,
            stdout=launcher.subprocess.DEVNULL,
            stderr=launcher.subprocess.DEVNULL,
            check=False,
            creationflags=0x08000000,
        )

    def test_hidden_child_requires_a_command(self) -> None:
        launcher = load_launcher()

        with patch.object(launcher.sys, "platform", "win32"):
            with self.assertRaisesRegex(ValueError, "requires a command"):
                launcher._run_windows_hidden_child([])


if __name__ == "__main__":
    unittest.main()
