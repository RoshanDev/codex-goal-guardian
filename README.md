# Codex Goal Guardian

Codex Goal Guardian keeps active Codex Goals recoverable after the built-in
reconnect limit. It deliberately uses two supported ownership paths:

- a native in-chat heartbeat for the Windows ChatGPT/Codex desktop app;
- an external recovery supervisor for native Windows Codex CLI and Codex CLI
  running inside WSL2 Ubuntu 22.04.

The CLI supervisor lives in stable user directories and uses the documented
Codex App Server JSON-RPC surface. It never takes ownership of desktop
`source=vscode` tasks. Desktop app updates do not overwrite the supervisor,
plugin, scheduled tasks, or in-chat heartbeat definition.

## Why this survives updates

The Windows app package, `app.asar`, private UI bridges, and versioned editor
extensions are outside the trust boundary. The desktop layer is a supported
scheduled follow-up attached to the same task and therefore keeps the task
context and runtime ownership in the app.

Windows Task Scheduler launches 15-second native Windows and WSL CLI watchers;
an optional WSL user timer is a fallback. A one-minute watchdog restarts either
watcher if it exits. All three Windows tasks enter through `pythonw.exe`; WSL
and PowerShell children use the Windows `CREATE_NO_WINDOW` flag, so scheduled
monitoring does not leave visible console windows. Native Codex App Server
recovery children use the same flag. The standalone runtime is copied to:

- Windows: `%LOCALAPPDATA%\CodexGoalGuardian`
- WSL2: `~/.local/share/codex-goal-guardian`

The bundled Codex plugin adds diagnostics, desktop-heartbeat guidance, and
privacy-filtered Stop evidence. This avoids the repair-after-every-update
behavior of main-process patchers such as Codex++.

## Recovery contract

For a desktop task, the in-chat heartbeat first checks whether another turn is
already `inProgress`. If so, it performs no work. It continues only when the
persisted Goal is still `active` and no turn is running, and it never archives,
hands off, blocks, completes, or rewrites that Goal.

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

Until the last two capabilities exist, a second external App Server cannot
safely own Desktop recovery. The same-task heartbeat is the supported desktop
path; the external supervisor remains CLI-only and fails closed.

## Requirements

- Python 3.10 or newer on each native runtime
- Codex CLI with `codex app-server`
- Windows PowerShell 5.1 or newer
- WSL2 Ubuntu 22.04 with a native Linux Codex executable

Do not install against a WSL command that resolves under `/mnt/c`. The WSL
installer rejects Windows shims explicitly.

## Install

### Windows desktop app

From the Goal task itself, ask ChatGPT/Codex to create a one-minute scheduled
follow-up in the current task with this durable contract:

```text
Keep this task's existing active Goal alive. On each run, first check the real
task state. If another turn is already inProgress, do nothing. Only when the
Goal is active and no turn is running, continue from the latest persisted
checkpoint without repeating completed mutations. Never stop, interrupt,
archive, hand off, pause, block, complete, shrink, or rewrite the Goal.
```

Use an in-chat scheduled follow-up, not a standalone scheduled task. Ordinary
desktop app upgrades preserve it because it is user automation state rather
than an app-package patch.

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

From Windows PowerShell, install the native runtime and both self-restarting
scheduled watchers:

```powershell
$WslUser = "replace-with-your-wsl-user"
& "\\wsl.localhost\Ubuntu-22.04\home\$WslUser\Developer\codex-goal-guardian\installers\windows\install.ps1" `
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
run-once --dry-run      Preview eligible actions without changing a thread
run-once                Perform one transition/recovery check
watch --interval 15     Run foreground checks
hook-record             Store a filtered Stop hook event
```

## Plugin

The repository includes a focused Codex plugin under
`plugin/codex-goal-guardian`. Its Skill routes desktop recovery to an in-chat
heartbeat and CLI recovery to the external supervisor. Its Stop hook delegates
to the external installation only when found. The hook has a two-second
subprocess timeout, ignores prompt and tool payloads, and always exits
successfully.

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
  next safe in-chat heartbeat; CLI continuity starts after the original CLI
  process exits and network recovery is confirmed.
- Guardian cannot continue work that is waiting for user input or approval.
- An incompatible future App Server schema fails closed; it never falls back
  to direct database edits or UI automation.
- The external supervisor intentionally rejects desktop `vscode` tasks because
  a second App Server cannot observe the desktop process's true live turn
  ownership.
- CLI recovery is conservative: any matching live native CLI process delays
  takeover, including a different task using that same configured executable.
- Health restoration must be observed by the scheduler. Installing while the
  network is already healthy does not synthesize an outage.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See `docs/plans/` for the design and implementation plan.
