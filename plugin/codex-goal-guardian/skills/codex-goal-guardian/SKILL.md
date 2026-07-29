---
name: codex-goal-guardian
description: Diagnose, install, inspect, or safely operate Codex Goal Guardian for Windows App/CLI and WSL2 CLI network recovery.
---

# Codex Goal Guardian

Use this skill when a user asks why an active Goal stopped after reconnect
attempts, whether Guardian is installed, or how to inspect its recovery state.

## Safety contract

- Treat an explicitly configured `desktop_thread_ids` allowlist as the
  preferred Desktop recovery authority. It is local, deterministic, and does
  not create model turns.
- Allow the external Guardian to change only a twice-validated blocked Goal to
  `active`. Require `source=vscode`, no `inProgress` turn, and an exact recent
  network failure from App Server state or the task's read-only session JSONL.
- Treat an explicit allowlist entry as opt-in authority to wake that task. After
  Goal reactivation, read thread and Goal repeatedly, call `thread/resume`, and
  call one deterministic `turn/start` only if the task remains idle. Stay
  attached until the continuation settles.
- Deduplicate direct Desktop recovery by failed turn ID and deterministic
  client message ID.
- Treat the external Guardian as the native Windows/WSL CLI recovery authority.
- Prefer `doctor`, `status`, and `run-once --dry-run` before live recovery.
- Never externally wake an unlisted `source=vscode` desktop task.
- Never recover while a matching native CLI process is still running.
- Never patch the desktop app package, edit Codex session databases or JSONL
  logs, or delete Codex configuration, authentication, sessions, or project
  files.
- A live `run-once` is appropriate only after an observed down-to-up health
  transition for CLI recovery. A watched or explicitly requested Desktop task
  may be processed on any confirmed healthy pass because short disconnects can
  occur between probes.
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

For a legacy same-task Desktop heartbeat, queue one idempotent request with:

```powershell
& "$env:LOCALAPPDATA\CodexGoalGuardian\runtime\scripts\run-windows.ps1" request-desktop-recovery --config "$env:LOCALAPPDATA\CodexGoalGuardian\config.json" --thread-id "<THREAD_ID>" --json
```

The heartbeat path consumes model tokens and the desktop app still renders a
generic completed-heartbeat entry even when the control packet omits its
`message` element. Prefer `desktop_thread_ids`; retain the heartbeat only as a
legacy fallback.

## Diagnosis order

1. For a desktop task, confirm its exact ID is present in the Windows target's
   `desktop_thread_ids`. Inspect the direct-recovery record and latest
   non-Guardian task completion. Do not create a heartbeat by default.
2. For CLI, run `doctor` for the affected native runtime.
3. Confirm the resolved WSL executable is a Linux path and not a Windows shim.
4. Inspect `status` for health, outage generation, and `recovery_pending`.
5. Inspect the Guardian JSONL log and scheduler/timer state.
6. If the App Server contract is unavailable, fail closed and report the
   compatibility issue.
