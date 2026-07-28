import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

function readStdin() {
  try {
    return readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

function commandForPlatform() {
  const explicit = process.env.CODEX_GOAL_GUARDIAN_HOOK_COMMAND;
  if (explicit) {
    return { executable: explicit, args: [] };
  }
  if (process.platform === "win32") {
    const localAppData =
      process.env.LOCALAPPDATA ??
      path.join(homedir(), "AppData", "Local");
    const runner = path.join(
      localAppData,
      "CodexGoalGuardian",
      "runtime",
      "scripts",
      "run-windows.ps1",
    );
    if (!existsSync(runner)) return null;
    return {
      executable: "powershell.exe",
      args: [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        runner,
      ],
    };
  }
  const dataRoot =
    process.env.XDG_DATA_HOME ?? path.join(homedir(), ".local", "share");
  const runner = path.join(
    dataRoot,
    "codex-goal-guardian",
    "bin",
    "codex-goal-guardian",
  );
  if (!existsSync(runner)) return null;
  return { executable: runner, args: [] };
}

try {
  const command = commandForPlatform();
  if (command) {
    const input = readStdin();
    spawnSync(
      command.executable,
      [...command.args, "hook-record", "--json"],
      {
        input,
        encoding: "utf8",
        timeout: 2000,
        windowsHide: true,
        stdio: ["pipe", "ignore", "ignore"],
      },
    );
  }
} catch {
  // Evidence capture must never block or change Codex stop behavior.
}

process.exit(0);
