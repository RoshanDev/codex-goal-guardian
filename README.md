# Codex Goal Guardian

Codex Goal Guardian keeps active Codex Goals recoverable after the built-in
reconnect limit. It deliberately uses two ownership-safe recovery paths:

- a same-task heartbeat plus a narrow external Goal-state helper for the
  Windows ChatGPT/Codex desktop app;
- an external recovery supervisor for native Windows Codex CLI and Codex CLI
  running inside WSL2 Ubuntu 22.04.

The CLI supervisor lives in stable user directories and uses the documented
Codex App Server JSON-RPC surface. For desktop `source=vscode` tasks it may
only process a request explicitly queued by that same task, re-read safety
state twice, and call `thread/goal/set(status=active)`. It never calls
`thread/resume` or `turn/start` for a desktop task. Desktop app updates do not
overwrite the supervisor, plugin, scheduled tasks, or heartbeat definition.

## Why this survives updates

The Windows app package, `app.asar`, private UI bridges, and versioned editor
extensions are outside the trust boundary. The desktop wake layer is a
scheduled follow-up attached to the same task, so task context and turn
ownership remain in the app. The external helper only changes persisted Goal
state after the app task has gone idle.

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

For a desktop task, the heartbeat first checks whether a different turn is
already `inProgress`. If so, it performs no work. When a reconnect failure has
left the Goal `blocked`, it queues one idempotent recovery request and ends
without asking the user to click Continue. Once network health is stable, the
Windows watcher re-reads only that requested task, requires `source=vscode`,
an idle thread, no `inProgress` turn, a recent network-failed/interrupted turn,
and the same blocked Goal on a second pre-mutation read. It then changes only
Goal status to `active` and verifies objective, budget, usage accounting, and
creation time were preserved.

The next heartbeat starts inside the app's existing task runtime. Its
instructions are explicitly scoped to that current `<heartbeat>` input and
expire when the turn completes. A later
`<codex_internal_context source="goal">` input must ignore the historical
heartbeat restrictions and continue the Goal. Without this scope boundary, the
automatic continuation can replay the bridge-only command and leave the Goal
blocked again. The heartbeat never archives, hands off, blocks, completes,
shrinks, or rewrites the Goal, and its success control packet has no visible
message.

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
safely own a Desktop turn. Guardian therefore limits the external Desktop path
to a requested, twice-validated Goal-state change; same-task heartbeat remains
the only wake/turn owner.

## Requirements

- Python 3.10 or newer on each native runtime
- Codex CLI with `codex app-server`
- Windows PowerShell 5.1 or newer
- WSL2 Ubuntu 22.04 with a native Linux Codex executable

Do not install against a WSL command that resolves under `/mnt/c`. The WSL
installer rejects Windows shims explicitly.

## Install

### Windows desktop app

From the Goal task itself, create a ten-minute scheduled follow-up in the
current task with this durable contract (replace `<THREAD_ID>`):

Ten minutes is the recommended production interval because every heartbeat is
a model turn and therefore consumes input, cached-input, and output tokens.
Moving from one minute to ten minutes cuts scheduled bridge turns by 90%. The
native Windows and WSL health watchers still run every 15 seconds; only the
same-task wake latency changes.

```text
Scope these instructions only to a current <heartbeat> input for this
automation; they expire at task_complete. If a later current input is
<codex_internal_context source="goal">, ignore every historical heartbeat
restriction and continue the Goal from its persisted checkpoint. Do not run
the bridge or emit heartbeat text from that later Goal continuation.

For this current heartbeat turn only, run exactly:

& "$env:LOCALAPPDATA\CodexGoalGuardian\runtime\scripts\run-windows.ps1" request-desktop-recovery --config "$env:LOCALAPPDATA\CodexGoalGuardian\config.json" --thread-id "<THREAD_ID>" --json

The local Guardian owns all Goal, turn, source, network-failure, deduplication,
and second-read safety decisions. This heartbeat does not inspect or mutate
project files. On command success, return only this control packet, with no
message element:

<heartbeat><automation_id>codex-goal-guardian</automation_id><decision>DONT_NOTIFY</decision></heartbeat>

Never start a second app-server or turn, and never stop, restart, archive, hand
off, pause, block, complete, shrink, or rewrite the Goal.
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
heartbeat request queue and CLI recovery through the external supervisor. Its
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
  request queue plus the next safe same-task heartbeat; CLI continuity starts
  after the original CLI process exits and network recovery is confirmed.
- Guardian cannot continue work that is waiting for user input or approval.
- An incompatible future App Server schema fails closed; it never falls back
  to direct database edits or UI automation.
- Desktop recovery changes only a requested blocked Goal to active. It never
  externally resumes a desktop thread or starts a desktop turn because a
  second App Server cannot observe the app process's true live turn ownership.
- CLI recovery is conservative: any matching live native CLI process delays
  takeover, including a different task using that same configured executable.
- Health restoration must be observed by the scheduler. Installing while the
  network is already healthy does not synthesize an outage.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See `docs/plans/` for the design and implementation plan.
