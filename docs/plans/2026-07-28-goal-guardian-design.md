# Codex Goal Guardian design

Date: 2026-07-28

## Goal contract

Build an update-resilient recovery layer for:

- the Windows ChatGPT/Codex desktop app;
- Codex CLI running natively on Windows;
- Codex CLI running in WSL2 Ubuntu 22.04.

After the network transitions from unavailable to healthy, recovery must
continue an active Goal without creating a second owner or overlapping turn.
It must not repeat a recovery action for the same thread and outage generation.

## Chosen approach

Split ownership along supported runtime boundaries:

- The Windows desktop app uses a native in-chat heartbeat attached to the same
  task. It inherits task context and does nothing while another turn is active.
- Native Windows and WSL2 CLIs use a standalone standard-library Python
  supervisor. It talks to a compatible Codex CLI through documented App Server
  JSON-RPC and rejects desktop `source=vscode` tasks.

The implementation never patches `app.asar`, clicks the UI, or writes Codex
SQLite state directly. Windows Task Scheduler hosts the CLI supervisor because
it survives desktop app updates and can start WSL when necessary. A WSL
systemd user timer is an optional CLI-only fallback.

## Alternatives considered

1. Codex++ tweak: can inject main-process code and repair its patch after an
   update, but depends on private app packaging and bridge details. Rejected as
   the reliability layer.
2. Hook only: a Stop hook can request another continuation, but cannot reliably
   wait through a long outage or restart a dead app process. Retained only for
   lightweight failure evidence.
3. External App Server for desktop tasks: a fresh App Server reports the
   desktop thread as locally `notLoaded` and cannot see the desktop process's
   live turn ownership. Rejected because it can create overlapping turns.
4. Fixed-interval in-chat heartbeat: update-safe and owns the existing desktop
   task context. Chosen for the desktop layer with a strict active-turn guard.

## Components

### Core engine

- probes configured network endpoints and optional local proxy ports;
- persists health state and increments an outage generation on a healthy to
  unhealthy transition;
- waits while any process matching the configured native CLI command is alive;
- starts one App Server subprocess per recovering runtime target and keeps that
  connection attached while a resumed or recovery turn remains active;
- initializes the JSON-RPC connection and feature-probes required methods;
- lists recent threads, reads Goals, and inspects the final turn;
- selects only `cli`/`exec` source threads with an active Goal, an
  idle/system-error/not-loaded status, and an explicit network-failed or
  network-interrupted final turn;
- rejects active turns, paused/blocked/limited/completed Goals, stale threads,
  non-network failures, desktop sources, and already-recovered outage/thread
  pairs;
- resumes the thread and waits on the same connection if resume makes it
  active; in that case it never starts a second turn;
- starts and follows one idempotent reconciliation turn only when resume leaves
  the thread idle.

### Runtime targets

- `windows`: native Windows Codex CLI and Windows `CODEX_HOME`; desktop tasks
  sharing that home are excluded by source;
- `wsl`: WSL Codex CLI and Linux `CODEX_HOME`.

The installer records absolute executable paths after verifying the CLI
version and App Server capability. It does not rely on the WSL non-interactive
PATH, which currently resolves a Windows shim under Node 12.

### Installers

- Windows PowerShell installer copies a self-contained runtime to
  `%LOCALAPPDATA%\CodexGoalGuardian`, writes a local JSON configuration, and
  creates Task Scheduler entries that enter through `pythonw.exe`. Native
  monitoring runs in that task-owned process; the WSL watcher stays attached
  as a synchronous child created with `CREATE_NO_WINDOW`, so task restarts
  still control the watcher process tree without visible console windows.
  Native Codex App Server children use the same creation flag.
- WSL installer copies the same Python package to
  `~/.local/share/codex-goal-guardian` and can install a systemd user timer.
- Uninstallers remove only Guardian-owned tasks, units, and installed files.
  They preserve Codex sessions, authentication, config, and project data.

### Plugin

The plugin contributes:

- a Skill that routes desktop tasks to native heartbeat recovery and CLI tasks
  to external doctor/install/status/recovery workflows;
- a Stop hook that records compact failure evidence without blocking normal
  turns;
- scripts that delegate to the externally installed Guardian.

## Recovery data flow

1. Two consecutive probes report unhealthy and record a new outage generation.
2. Later probes must report healthy twice consecutively.
3. Guardian acquires the per-target state lock and proves no configured native
   CLI process is still running.
4. Guardian queries App Server and computes eligible CLI threads.
5. Re-read thread and Goal to close the race with a user or another client.
6. Reject non-CLI sources and non-network terminal turns.
7. Call `thread/resume`.
8. If resume makes the thread active, keep that App Server connection open
   until the turn settles and do not call `turn/start`.
9. If the thread remains idle and the Goal remains active, call `turn/start` with a
   deterministic recovery prompt and `clientUserMessageId` derived from target,
   outage generation, and thread ID, then stay attached until completion.
10. Persist the successful action before releasing the lock.

For desktop tasks, the in-chat heartbeat is scheduled independently. A run
continues only when the Goal is active and no other turn is `inProgress`.

## Failure behavior

- App Server method/schema mismatch: fail closed and emit a compatibility
  error; never fall back to SQLite mutation.
- Network remains unhealthy: no Codex process is started.
- Approval request during a recovery turn: leave the thread waiting for the
  user and do not retry the same outage generation.
- Multiple guardians: the state lock permits one recovery writer.
- Existing native CLI process: keep the outage recovery pending and retry later.
- Desktop source: fail closed and leave recovery to the same-task heartbeat.
- Corrupt Guardian state: preserve the bad file, start in observe-only mode,
  and require an explicit repair command.

## Acceptance criteria

1. Unit tests cover health transitions, eligibility, idempotency, stale/running
   thread rejection, and network-error classification.
2. A fake App Server integration test proves initialize, list, Goal read,
   thread re-read, resume, and optional turn start ordering.
3. Windows and WSL installer dry-runs produce stable external paths and never
   point at versioned Windows App package directories.
4. `doctor` identifies the current WSL zsh/native Codex versus the broken
   Windows shim resolved by non-interactive bash.
5. Plugin validation passes.
6. Repository contains no credentials, auth files, session logs, or machine
   state.
7. A public GitHub repository contains no credentials or local machine state,
   and the verified commit is pushed.
