# Codex Goal Guardian

Codex Goal Guardian keeps active Codex Goals recoverable after the built-in
reconnect limit. It deliberately uses two ownership-safe recovery paths:

- an explicit local thread allowlist plus an opt-in external recovery supervisor
  for the Windows ChatGPT/Codex desktop app;
- an external recovery supervisor for native Windows Codex CLI and Codex CLI
  running inside WSL2 Ubuntu 22.04.

The CLI supervisor lives in stable user directories and uses the documented
Codex App Server JSON-RPC surface. For desktop `source=vscode` tasks it may
only inspect IDs explicitly pinned in `desktop_thread_ids` or process a legacy
same-task request. It re-reads safety state twice and calls only
`thread/goal/set(status=active)` for the legacy path. An explicitly allowlisted
task also opts into a guarded `thread/resume` plus one deterministic
`turn/start` when the app has left the task idle. Desktop app updates do not
overwrite the supervisor, plugin, scheduled tasks, configuration, or recovery
state.

## Why this survives updates

The Windows app package, `app.asar`, private UI bridges, and versioned editor
extensions are outside the trust boundary. The external helper takes over only
an explicitly allowlisted task after the app has left it idle because of an
exact persisted network failure. It keeps the recovery App Server attached
while the continuation runs.

Windows Task Scheduler launches 15-second native Windows and WSL CLI watchers;
an optional WSL user timer is a fallback. A one-minute watchdog restarts either
watcher if it exits. All three Windows tasks enter through `pythonw.exe`; WSL
and PowerShell children use the Windows `CREATE_NO_WINDOW` flag, so scheduled
monitoring does not leave visible console windows. Native Codex App Server
recovery children use the same flag. The standalone runtime is copied to:

- Windows: `%LOCALAPPDATA%\CodexGoalGuardian`
- WSL2: `~/.local/share/codex-goal-guardian`

The bundled Codex plugin adds diagnostics, local-watch guidance, and
privacy-filtered Stop evidence. This avoids the repair-after-every-update
behavior of main-process patchers such as Codex++.

## Recovery contract

For a desktop task, the Windows watcher examines only configured
`desktop_thread_ids`. It requires `source=vscode`, an idle thread, no
`inProgress` turn, a blocked Goal, and a network-failed latest non-Guardian
turn. Codex App Server can normalize some failed remote-compaction turns to
`completed`; Guardian therefore checks the matching `task_complete` event in
the task's append-only session JSONL when the App Server error is empty.
Session logs are read-only and never edited.

Immediately before mutation the watcher reads the thread and Goal again. It
changes only Goal status to `active`, verifies objective, budget, usage
accounting, and creation time were preserved, and records the failure turn ID.
It then waits, reads thread and Goal twice again, calls `thread/resume`, and
allows the desktop runtime two more observations to wake itself. If the task
remains idle, Guardian starts exactly one continuation using a deterministic
client message ID and stays attached until that turn settles. The same failed
turn cannot start the same continuation twice.

This direct watcher is ordinary local code. Its probes, state checks, and
Goal-state mutation do not create model turns or consume tokens. The actual
recovered continuation is a normal Goal turn and consumes only the tokens
needed to continue the work. A legacy same-task heartbeat request remains
supported, but every heartbeat itself is an extra model turn and the desktop
app can render a generic “Heartbeat completed quietly.” entry even when
`DONT_NOTIFY` has no message payload.

Guardian requires two consecutive failed probes before declaring an outage,
then two consecutive healthy probes before recovery. A single TLS/proxy blip
does not create an outage generation. Before opening App Server, it defers
while any process matching the configured native CLI command still exists.
This prevents takeover while the original Windows or WSL CLI is reconnecting
or working. It then considers only recent threads with:

- source `cli` or `exec` (configurable with `allowed_sources`);
- a persisted Goal whose status is `active`;
- thread status `idle`, `systemError`, or `notLoaded`;
- a final turn whose status is `failed` or `interrupted` and whose error
  explicitly matches a network/transport failure;
- no completed action for the same target, outage generation, and thread.

Immediately before mutation it reads the thread and Goal again. It calls
`thread/resume` and waits briefly. If resume already brings a turn online, the
same App Server connection follows that turn until it settles and never starts
a second turn. If the thread remains idle, Guardian starts one deterministic
reconciliation turn and stays attached until it settles. Successful stages are
persisted atomically. It uses a deterministic client message ID so retries
remain idempotent.

The reconciliation prompt tells Codex to inspect recorded terminal state and
avoid repeating successful commands or mutations.

## Upstream status

This project works around open upstream gaps rather than claiming to fix the
underlying transport:

- [openai/codex#29087](https://github.com/openai/codex/issues/29087) tracks
  incomplete long-running tasks after a streamed transport disconnect.
- [openai/codex#18471](https://github.com/openai/codex/issues/18471) documents
  Windows Desktop `Reconnecting... N/5` cases caused by turn/conversation state
  races even when App Server transport remains connected.
- [openai/codex#25914](https://github.com/openai/codex/issues/25914) requests a
  supported way for external clients to discover and attach to the live Desktop
  thread. Persisted thread visibility currently does not prove live-turn
  ownership.
- [openai/codex#35676](https://github.com/openai/codex/issues/35676) requests a
  read-only subscriber-presence signal so supervisors can distinguish a loaded
  thread with an interactive owner from an orphaned loaded thread.

Until the last two capabilities exist, there is no perfect subscriber-presence
proof. Guardian therefore makes external Desktop wake an explicit allowlist
opt-in, rejects any active/in-progress task, performs repeated safety reads,
and uses a deterministic start ID. The legacy request path remains Goal-state
only.

## Requirements

- Python 3.10 or newer on each native runtime
- Codex CLI with `codex app-server`
- Windows PowerShell 5.1 or newer
- WSL2 Ubuntu 22.04 with a native Linux Codex executable

Do not install against a WSL command that resolves under `/mnt/c`. The WSL
installer rejects Windows shims explicitly.

## Install

### Windows desktop app

Pass each desktop task ID to the Windows installer. The list is explicit and
update-safe:

```powershell
& ".\installers\windows\install.ps1" `
  -DesktopThreadId "019f..." `
  -WslDistro "Ubuntu-22.04" `
  -WslUser "your-wsl-user"
```

Pass several IDs as a PowerShell array when guarding more than one task.
Rerunning the installer without `-DesktopThreadId` preserves the existing
allowlist. Passing it again replaces the allowlist intentionally.

The legacy `request-desktop-recovery` heartbeat path remains available for
installations that do not configure an allowlist. It is no longer recommended
for production because it consumes tokens and creates visible task-history
entries.

### Windows and WSL2 CLI

Clone or open this repository inside WSL2, then install the WSL runtime:

```bash
cd ~/Developer/codex-goal-guardian
./installers/wsl/install.sh \
  --codex-command ~/.bun/bin/codex
```

Add `--with-systemd` when you want the optional WSL-only timer. The Windows WSL
watcher already starts the distro and checks every 15 seconds, so the timer is
a fallback rather than a requirement. Configure a proxy only when it is
reachable from WSL; Windows loopback proxy ports are not always forwarded.
For a Windows proxy listening on port 7890, verify the hostname first, then use:

```bash
./installers/wsl/install.sh \
  --codex-command ~/.bun/bin/codex \
  --node-command "$(command -v node)" \
  --proxy-url http://host.docker.internal:7890 \
  --tcp-host host.docker.internal \
  --tcp-port 7890
```

The explicit Node path also prevents a scheduled non-interactive WSL process
from resolving an older system Node instead of the Node used by Codex.

From Windows PowerShell, install the native runtime and both self-restarting
scheduled watchers:

```powershell
$WslUser = "replace-with-your-wsl-user"
& "\\wsl.localhost\Ubuntu-22.04\home\$WslUser\Developer\codex-goal-guardian\installers\windows\install.ps1" `
  -DesktopThreadId "<THREAD_ID>" `
  -ProxyUrl "http://127.0.0.1:7890" `
  -TcpHost "127.0.0.1" `
  -TcpPort 7890 `
  -WslDistro "Ubuntu-22.04" `
  -WslUser $WslUser `
  -WatchIntervalSeconds 15
```

Both installers support a read-only `--dry-run` / `-DryRun`. They preserve an
existing configuration unless `--force-config` / `-ForceConfig` is supplied.

## Verify

Windows:

```powershell
& "$env:LOCALAPPDATA\CodexGoalGuardian\runtime\scripts\run-windows.ps1" `
  doctor --config "$env:LOCALAPPDATA\CodexGoalGuardian\config.json" --json
```

WSL2:

```bash
~/.local/share/codex-goal-guardian/bin/codex-goal-guardian \
  doctor --config ~/.config/codex-goal-guardian/config.json --json
```

Useful commands:

```text
doctor                  Validate health, native CLI, and a read-only App Server RPC
status                  Read Guardian-owned state
request-desktop-recovery Queue one idempotent same-task Desktop request
run-once --dry-run      Preview eligible actions without changing a thread
run-once                Perform one transition/recovery check
watch --interval 15     Run foreground checks
hook-record             Store a filtered Stop hook event
```

## Plugin

The repository includes a focused Codex plugin under
`plugin/codex-goal-guardian`. Its Skill routes desktop recovery through the
local allowlisted watcher and CLI recovery through the external supervisor. Its
Stop hook delegates to the external installation only when found. The hook has
a two-second subprocess timeout, ignores prompt and tool payloads, and always
exits successfully.

Register this repository as a local marketplace, then install the plugin:

```bash
codex plugin marketplace add ~/Developer/codex-goal-guardian
codex plugin add codex-goal-guardian@codex-goal-guardian-local
```

Start a new task after plugin installation so Codex loads the new Skill and
Hook. App updates do not change the external runtime or scheduled tasks.

## Updating and uninstalling

Pull a new repository revision and rerun the installers. Runtime files and
owned scheduler definitions are refreshed; local configuration and state are
preserved by default.

The Windows installer checks both native Windows and WSL recovery processes
before it stops any Guardian-owned task. The default
`-DrainTimeoutMinutes 0` fails closed when a recovery is active. For an
unattended rolling update, let it wait for the active recovery to finish
naturally:

```powershell
.\installers\windows\install.ps1 `
  -WslDistro Ubuntu-22.04 `
  -WslUser "your-wsl-user" `
  -DrainTimeoutMinutes 720
```

The installer requires two consecutive clear observations before changing the
runtime or scheduler. A timeout also exits without stopping any task.

Uninstallers remove only Guardian-owned tasks, units, and runtime files:

```powershell
.\installers\windows\uninstall.ps1
```

```bash
./installers/wsl/uninstall.sh
```

Use `-PurgeData` or `--purge-data` only when configuration, idempotency state,
and logs should also be deleted.

## Limitations

- Codex can still display its built-in `reconnecting /5` sequence. Guardian
  does not replace that UI transport stream. Desktop continuity comes from the
  local Goal-state watcher; CLI continuity starts after the original CLI
  process exits and network recovery is confirmed.
- Guardian cannot continue work that is waiting for user input or approval.
- An incompatible future App Server schema fails closed; it never falls back
  to direct database edits or UI automation.
- An explicit `desktop_thread_ids` entry opts that task into external wake.
  Guardian changes only a network-blocked Goal to active, repeatedly rejects a
  live/in-progress runtime, resumes the persisted task, and starts at most one
  deterministic continuation if it remains idle. The absence of an upstream
  subscriber-presence signal leaves a small residual ownership race.
- CLI recovery is conservative: any matching live native CLI process delays
  takeover, including a different task using that same configured executable.
- Health restoration must be observed by the scheduler. Installing while the
  network is already healthy does not synthesize an outage.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See `docs/plans/` for the design and implementation plan.
