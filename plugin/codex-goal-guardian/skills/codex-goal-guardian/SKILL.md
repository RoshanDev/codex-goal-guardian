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
- Require the Desktop target's `app_server_url` to equal the user-level
  `CODEX_APP_SERVER_WS_URL`, and require the hidden shared App Server task to be
  running. A separate stdio App Server is not Desktop Goal recovery.
- Without delegated continuity, change only a twice-validated network-blocked
  Goal to `active`. With explicit `delegated_continuity_enabled`, treat the
  thread allowlist as frozen-scope delegation and continue idle blocked or
  usage-limited Goals after completed, failed, or interrupted turns. Never
  wake a complete, paused, budget-limited, or `inProgress` Goal. Network
  evidence may come from App Server state or the read-only session JSONL.
- Treat an explicit allowlist entry as opt-in authority to wake that task. After
  Goal reactivation, read thread and Goal repeatedly, call `thread/resume`, and
  call one deterministic `turn/start` only if the task remains idle. Stay
  attached until the continuation settles.
- Deduplicate direct Desktop recovery by failed turn ID and deterministic
  client message ID.
- For delegated continuity, make in-scope decisions without intermediate user
  approval, while never inventing new scope or replaying completed/uncertain
  exact-once external actions.
- Treat the external Guardian as the native Windows/WSL CLI recovery authority.
- For the exact `Selected model is at capacity. Please try a different model.`
  failure on a CLI or explicitly allowlisted Desktop target, require a
  non-empty configured `model_capacity_fallback_models` list, an active Goal,
  an idle allowed-source thread, and no `inProgress` turn. A CLI target also
  requires that no matching native CLI process exists; a Desktop target also
  requires its configured shared App Server.
- Retry the thread's existing model before any fallback. Preserve the user's
  last reasoning effort by omitting `effort` from `turn/start`. Retry each
  model up to `model_capacity_retry_limit` times with persisted exponential
  backoff, capped by `model_capacity_backoff_max_seconds`, before advancing to
  the next explicitly configured model.
- Never invent a fallback model, reset the persisted retry ledger on watcher
  restart, or retry before `next_retry_at`. Fail closed after the configured
  model list is exhausted.
- Prefer `doctor`, `status`, and `run-once --dry-run` before live recovery.
- Never externally wake an unlisted `source=vscode` desktop task.
- Never recover while a matching native CLI process is still running.
- Never replay an exact platform `Invalid prompt: your prompt was flagged`
  input or search behind it for older network evidence. For an explicitly
  allowlisted Desktop target with `prompt_policy_retry_enabled`, start at most
  one new fixed continuation with a deterministic message ID. If that new turn
  receives the same rejection, fail closed as `prompt_policy_retry_exhausted`.
- Never patch the desktop app package, edit Codex session databases or JSONL
  logs, or delete Codex configuration, authentication, sessions, or project
  files.
- If ChatGPT startup fails with `ECONNREFUSED` to the exact configured
  `CODEX_APP_SERVER_WS_URL`, confirm the shared App Server is unavailable and
  read `desktop-environment-backup.json`. Restore only the backed-up user
  environment value (or remove the variable when the backup says it was
  absent), then relaunch only ChatGPT. Do not stop a Windows/WSL Guardian or
  Codex CLI process. Report that Desktop shared-runtime recovery remains
  disabled until its listener is repaired and the installer is rerun.
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
   `desktop_thread_ids`. Confirm the Desktop process and Guardian use the same
   loopback App Server, then inspect the direct-recovery record and latest
   non-Guardian task completion. Do not create a heartbeat by default.
   If the app cannot reach that listener and fails before showing its main
   window, use the backed-up environment value as the fail-open recovery path;
   never guess or delete unrelated user environment variables.
2. For CLI, run `doctor` for the affected native runtime.
3. Confirm the resolved WSL executable is a Linux path and not a Windows shim.
4. Inspect `status` for health, outage generation, and `recovery_pending`.
5. Inspect the Guardian JSONL log and scheduler/timer state.
6. For a capacity failure, inspect the target's ordered fallback list and its
   `model_capacity_recoveries` state record. Confirm the default model receives
   its full retry budget before a fallback model is selected.
7. If the App Server contract is unavailable, fail closed and report the
   compatibility issue.
