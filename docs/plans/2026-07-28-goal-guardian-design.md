# Codex Goal Guardian design

Date: 2026-07-28

## Goal contract

Build an update-resilient recovery layer for:

- the Windows ChatGPT/Codex desktop app;
- Codex CLI running natively on Windows;
- Codex CLI running in WSL2 Ubuntu 22.04.

After the network transitions from unavailable to healthy, the guardian must
resume only eligible threads whose persisted Goal is still active and whose
last turn is no longer running. It must not repeat a recovery action for the
same thread and outage generation.

## Chosen approach

Use a standalone, standard-library Python supervisor as the authority. Package
Codex Skill and Hook files as a supported plugin, but keep the supervisor and
its state outside the desktop app installation.

The supervisor talks to a compatible Codex CLI through the documented App
Server JSON-RPC interface. It never patches `app.asar`, clicks the UI, or writes
Codex SQLite state directly.

Windows Task Scheduler is the primary host because it survives desktop app
updates and can start WSL when necessary. A WSL systemd user timer is provided
as an optional CLI-only fallback.

## Alternatives considered

1. Codex++ tweak: can inject main-process code and repair its patch after an
   update, but depends on private app packaging and bridge details. Rejected as
   the reliability layer.
2. Hook only: a Stop hook can request another continuation, but cannot reliably
   wait through a long outage or restart a dead app process. Retained only for
   lightweight failure evidence.
3. Fixed-interval thread heartbeat only: update-safe and useful as a fallback,
   but it is timer-driven rather than network-transition-driven. Retained as an
   optional second recovery layer.

## Components

### Core engine

- probes configured network endpoints and optional local proxy ports;
- persists health state and increments an outage generation on a healthy to
  unhealthy transition;
- starts one App Server subprocess per recovering runtime target and keeps that
  connection attached while a resumed or recovery turn remains active;
- initializes the JSON-RPC connection and feature-probes required methods;
- lists recent threads, reads Goals, and inspects the final turn;
- selects only active Goal + idle/system-error/not-loaded threads;
- rejects active turns, paused/blocked/limited/completed Goals, stale threads,
  and already-recovered outage/thread pairs;
- resumes the thread, waits on the same connection if resume makes it active,
  then starts and follows an idempotent reconciliation turn only when needed.

### Runtime targets

- `windows`: native Windows Codex CLI and Windows `CODEX_HOME`;
- `wsl`: WSL Codex CLI and Linux `CODEX_HOME`.

The installer records absolute executable paths after verifying the CLI
version and App Server capability. It does not rely on the WSL non-interactive
PATH, which currently resolves a Windows shim under Node 12.

### Installers

- Windows PowerShell installer copies a self-contained runtime to
  `%LOCALAPPDATA%\CodexGoalGuardian`, writes a local JSON configuration, and
  creates Task Scheduler entries that supervise native Python and `wsl.exe`
  directly so task restarts do not orphan child watchers.
- WSL installer copies the same Python package to
  `~/.local/share/codex-goal-guardian` and can install a systemd user timer.
- Uninstallers remove only Guardian-owned tasks, units, and installed files.
  They preserve Codex sessions, authentication, config, and project data.

### Plugin

The plugin contributes:

- a Skill for doctor/install/status/recovery workflows;
- a Stop hook that records compact failure evidence without blocking normal
  turns;
- scripts that delegate to the externally installed Guardian.

## Recovery data flow

1. Two consecutive probes report unhealthy and record a new outage generation.
2. Later probes must report healthy twice consecutively.
3. Guardian queries App Server and computes eligible threads.
4. For each eligible thread, acquire an atomic per-target lock.
5. Re-read thread and Goal to close the race with a user or another client.
6. Call `thread/resume`.
7. If resume makes the thread active, keep that App Server connection open
   until the turn settles.
8. If configured and the Goal remains active, call `turn/start` with a
   deterministic recovery prompt and `clientUserMessageId` derived from target,
   outage generation, and thread ID, then stay attached until completion.
9. Persist the successful action before releasing the lock.

## Failure behavior

- App Server method/schema mismatch: fail closed and emit a compatibility
  error; never fall back to SQLite mutation.
- Network remains unhealthy: no Codex process is started.
- Approval request during a recovery turn: leave the thread waiting for the
  user and do not retry the same outage generation.
- Multiple guardians: the state lock permits one recovery writer.
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
7. A private GitHub repository is created and the verified commit is pushed.
