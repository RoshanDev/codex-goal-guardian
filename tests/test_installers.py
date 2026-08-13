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
        self.assertIn("CodexGoalGuardian-AppServer", content)
        self.assertIn("[switch]$DryRun", content)
        self.assertIn("Register-ScheduledTask", content)
        self.assertIn("watch --config", content)
        self.assertIn("guardian-launch.py", content)
        self.assertIn("Resolve-NativePython", content)
        self.assertIn("pythonw.exe", content)
        self.assertIn("[int]$DrainTimeoutMinutes", content)
        self.assertIn("Wait-GuardianRecoveryDrain", content)
        self.assertIn('"maintenance.lock"', content)
        self.assertIn("trap {", content)
        self.assertIn("Get-CimInstance Win32_Process", content)
        self.assertIn("/proc/[0-9]*/cmdline", content)
        self.assertIn("is_guardian_watch_descendant", content)
        self.assertIn("codex_goal_guardian*watch*--config", content)
        self.assertIn("Select-Object -First 1", content)
        self.assertIn("$ClearObservations -ge 2", content)
        self.assertIn("$ProbeFailureAnnounced", content)
        self.assertIn("Treating the probe as active", content)
        self.assertIn("No tasks were stopped", content)
        self.assertIn("Update-GuardianConfig", content)
        self.assertIn("windows-desktop-goal-state", content)
        self.assertIn('recovery_mode = "desktop_goal_state"', content)
        self.assertIn("app_server_url", content)
        self.assertIn("CODEX_APP_SERVER_WS_URL", content)
        self.assertIn("ws://127.0.0.1:", content)
        self.assertIn(
            '$SharedAppServerListenUrl = "ws://127.0.0.1:$AppServerPort"',
            content,
        )
        self.assertIn(
            '$SharedAppServerUrl = "ws://127.0.0.1:$AppServerPort/rpc"',
            content,
        )
        self.assertIn("Set-DesktopAppServerEnvironment", content)
        self.assertIn('allowed_sources = @("vscode")', content)
        self.assertIn("[string[]]$DesktopThreadId", content)
        self.assertIn("desktop_thread_ids", content)
        self.assertIn("delegated_continuity_enabled", content)
        self.assertIn("$ReplaceDesktopThreadIds", content)
        self.assertIn("$DesktopWakeEnabled", content)
        self.assertIn(
            "start_recovery_turn = $DesktopWakeEnabled",
            content,
        )
        self.assertIn("pre-0.3.0.bak", content)
        self.assertIn("__disabled_until_guardian_upgrade__", content)
        self.assertIn("2592000", content)
        self.assertLess(
            content.rindex("Wait-GuardianRecoveryDrain"),
            content.index("Stop-ScheduledTask"),
        )
        self.assertEqual(
            content.count("New-ScheduledTaskAction -Execute $PythonwPath"),
            4,
        )
        self.assertGreaterEqual(content.count("--windows-hidden-child"), 2)
        self.assertIn("allowed_sources", content)
        self.assertNotIn('New-ScheduledTaskAction -Execute $WslPath', content)
        self.assertNotIn(
            'New-ScheduledTaskAction -Execute $PowerShellPath',
            content,
        )
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
        self.assertIn('"allowed_sources"', content)
        self.assertIn('"recovery_mode": "cli_turn"', content)
        self.assertIn('"delegated_continuity_enabled": True', content)
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
        self.assertIn("CodexGoalGuardian-AppServer", windows)
        self.assertIn("CODEX_APP_SERVER_WS_URL", windows)
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
        self.assertIn("CREATE_NO_WINDOW", content)
        self.assertIn('"maintenance.lock"', content)
        self.assertNotIn("WindowsApps", content)
        self.assertNotIn("resources/app", content)


if __name__ == "__main__":
    unittest.main()
