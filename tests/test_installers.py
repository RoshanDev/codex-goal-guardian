from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallerInvariantTests(unittest.TestCase):
    def test_windows_installer_uses_stable_user_path_and_owned_tasks(self) -> None:
        content = (ROOT / "installers/windows/install.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("$env:LOCALAPPDATA", content)
        self.assertIn("CodexGoalGuardian-Windows", content)
        self.assertIn("CodexGoalGuardian-WSL", content)
        self.assertIn("CodexGoalGuardian-Watchdog", content)
        self.assertIn("[switch]$DryRun", content)
        self.assertIn("Register-ScheduledTask", content)
        self.assertIn("watch --config", content)
        self.assertIn("guardian-launch.py", content)
        self.assertIn("Resolve-NativePython", content)
        self.assertIn('New-ScheduledTaskAction -Execute $PythonPath', content)
        self.assertIn('New-ScheduledTaskAction -Execute $WslPath', content)
        self.assertIn("[TimeSpan]::Zero", content)
        self.assertNotIn("WindowsApps", content)
        self.assertNotIn(".vscode", content.lower())

    def test_wsl_installer_uses_xdg_paths_and_optional_user_timer(self) -> None:
        content = (ROOT / "installers/wsl/install.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("XDG_DATA_HOME", content)
        self.assertIn("XDG_CONFIG_HOME", content)
        self.assertIn("codex-goal-guardian.timer", content)
        self.assertIn("--dry-run", content)
        self.assertIn("--with-systemd", content)
        self.assertIn("--node-command", content)
        self.assertIn("GUARDIAN_COMMAND_1", content)
        self.assertNotIn("/mnt/c/", content)

    def test_uninstallers_remove_only_guardian_owned_names(self) -> None:
        windows = (ROOT / "installers/windows/uninstall.ps1").read_text(
            encoding="utf-8"
        )
        wsl = (ROOT / "installers/wsl/uninstall.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("CodexGoalGuardian-Windows", windows)
        self.assertIn("CodexGoalGuardian-WSL", windows)
        self.assertIn("CodexGoalGuardian-Watchdog", windows)
        self.assertIn("codex-goal-guardian", wsl)
        self.assertNotIn(".codex", windows)
        self.assertNotIn('rm -rf "$HOME"', wsl)
        self.assertNotIn("Remove-Item $HOME", windows)

    def test_runners_do_not_depend_on_versioned_app_paths(self) -> None:
        content = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "scripts/run-windows.ps1",
                "scripts/run-wsl.sh",
                "scripts/run-wsl-from-windows.ps1",
                "scripts/watchdog-windows.ps1",
                "scripts/guardian-launch.py",
            )
        )

        self.assertIn("PYTHONPATH", content)
        self.assertIn("sys.executable", content)
        self.assertNotIn("WindowsApps", content)
        self.assertNotIn("resources/app", content)


if __name__ == "__main__":
    unittest.main()
