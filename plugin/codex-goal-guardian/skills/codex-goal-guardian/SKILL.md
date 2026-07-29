---
name: codex-goal-guardian
description: Diagnose, install, inspect, or safely operate Codex Goal Guardian for Windows App/CLI and WSL2 CLI network recovery.
---

# Codex Goal Guardian

Use this skill when a user asks why an active Goal stopped after reconnect
attempts, whether Guardian is installed, or how to inspect its recovery state.

## Safety contract

- Treat the same-task heartbeat as the desktop wake/turn authority.
- Allow the external Guardian to process only an explicit same-task Desktop
  request and change only a twice-validated blocked Goal to `active`.
- Treat the external Guardian as the native Windows/WSL CLI recovery authority.
- Prefer `doctor`, `status`, and `run-once --dry-run` before live recovery.
- Never call `thread/resume` or `turn/start` externally for a `source=vscode`
  desktop task.
- Never recover while a matching native CLI process is still running.
- Never patch the desktop app package, edit Codex session databases, or delete
  Codex configuration, authentication, sessions, or project files.
- A live `run-once` is appropriate only after an observed down-to-up health
  transition for CLI recovery. A queued Desktop request may be processed on
  any confirmed healthy pass because short disconnects can occur between
  probes.
- Report the target, outage generation, thread ID, and action without exposing
  prompt text or authentication data.

## Stable commands

Windows:

```powershell
& "$env:LOCALAPPDATA\CodexGoalGuardian\runtime\scripts\run-windows.ps1" doctor --config "$env:LOCALAPPDATA\CodexGoalGuardian\config.json" --json
```

WSL2:

```bash
~/.local/share/codex-goal-guardian/bin/codex-goal-guardian doctor \
  --config ~/.config/codex-goal-guardian/config.json --json
```

For a read-only recovery preview, replace `doctor` with
`run-once --dry-run`. Use `status` to inspect only Guardian-owned state.

From a same-task Desktop heartbeat, queue one idempotent request with:

```powershell
& "$env:LOCALAPPDATA\CodexGoalGuardian\runtime\scripts\run-windows.ps1" request-desktop-recovery --config "$env:LOCALAPPDATA\CodexGoalGuardian\config.json" --thread-id "<THREAD_ID>" --json
```

Queue only when the Goal is blocked after a recent network/stream/TLS failure
and no different pre-existing turn is `inProgress`. Never tell the user to
click Continue. End the heartbeat silently after queuing. When the Goal is
already active, also end silently so the app runtime's Goal-idle continuation
owns the next turn.

## Diagnosis order

1. For a desktop task, inspect or create one same-task heartbeat whose first
   rule is to do nothing when a different turn is already `inProgress`; its
   blocked-Goal branch must queue `request-desktop-recovery`, never request a
   manual click.
2. For CLI, run `doctor` for the affected native runtime.
3. Confirm the resolved WSL executable is a Linux path and not a Windows shim.
4. Inspect `status` for health, outage generation, and `recovery_pending`.
5. Inspect the Guardian JSONL log and scheduler/timer state.
6. If the App Server contract is unavailable, fail closed and report the
   compatibility issue.
