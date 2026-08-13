# Codex Goal Guardian

Codex Goal Guardian keeps active Codex Goals recoverable after the built-in
reconnect limit. It deliberately uses two ownership-safe recovery paths:

- a shared loopback App Server used by both the Windows ChatGPT/Codex desktop
  app and an explicit local thread allowlist watcher;
- an external recovery supervisor for native Windows Codex CLI and Codex CLI
  running inside WSL2 Ubuntu 22.04.

The supervisor lives in stable user directories and uses the Codex App Server
JSON-RPC surface. The Windows installer starts one hidden App Server at
`ws://127.0.0.1:<port>/rpc` and persists the desktop app's supported
`CODEX_APP_SERVER_WS_URL` setting. Guardian connects to that exact server
instead of launching a second stdio runtime. A Goal update is therefore
observable by the desktop renderer as `thread/goal/updated`, and the bottom
Goal card and Guardian read the same state. Desktop app updates do not
overwrite the user environment setting, supervisor, plugin, scheduled tasks,
configuration, or recovery state.

The Windows runtime validates the persisted per-user setting directly from
`HKCU\Environment`. Task Scheduler therefore does not depend on inheriting the
environment snapshot that existed before Guardian was installed or upgraded.

## Why this survives updates

The Windows app package, `app.asar`, private UI bridges, and versioned editor
extensions are outside the trust boundary. Guardian uses the desktop app's
supported App Server WebSocket setting and never patches the package. It acts
only on an explicitly allowlisted task after the shared runtime has left it
idle because of an exact persisted network failure.

Windows Task Scheduler launches 15-second native Windows and WSL CLI watchers;
an optional WSL user timer is a fallback. A fourth Windows task hosts the
shared loopback App Server, and a one-minute watchdog restarts the server or
either watcher if it exits. All four Windows tasks enter through
`pythonw.exe`; App Server, WSL, and PowerShell children use the Windows
`CREATE_NO_WINDOW` flag, so scheduled monitoring does not leave visible
console windows. The standalone runtime is copied to:

- Windows: `%LOCALAPPDATA%\CodexGoalGuardian`
- WSL2: `~/.local/share/codex-goal-guardian`

During an in-place upgrade the installer holds a local maintenance marker.
The watchdog exits quietly while that marker belongs to a live installer, so
it cannot restart an old watcher during runtime replacement. A stale marker is
removed automatically after an interrupted installer exits.

The bundled Codex plugin adds diagnostics, local-watch guidance, and
privacy-filtered Stop evidence. This avoids the repair-after-every-update
behavior of main-process patchers such as Codex++.

## Recovery contract

For a desktop task, the Windows watcher examines only configured
`desktop_thread_ids`. By default it retains the conservative network-failure
contract. When `delegated_continuity_enabled` is explicitly enabled, the
allowlist is also the durable delegation boundary: an idle `source=vscode`
Goal is continued after completed, failed, interrupted, blocked, or
usage-limited turns until the Goal becomes complete, paused/cancelled, or
budget-limited. It never wakes an `inProgress` turn. Codex App Server can
normalize some failed remote-compaction turns to `completed`; Guardian can
also check matching `task_complete` evidence in the read-only session JSONL.

The desktop target is invalid without `app_server_url`; Guardian fails closed
instead of silently spawning the separate stdio runtime that cannot update the
native Goal card. During a 0.5.x migration it also detects the old packaged
`app-server` child and waits until the Desktop app has restarted onto the
shared runtime. Immediately before mutation the watcher reads the thread and
Goal again. It
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

For native CLI targets and explicitly allowlisted Desktop targets, Guardian can
also recover the exact persisted error
`Selected model is at capacity. Please try a different model.` when
`model_capacity_fallback_models` is non-empty. It first keeps the thread's
current model and reasoning effort for 10 retries. Retries use persisted
exponential backoff starting at 15 seconds and cap at 600 seconds. Only after
the per-model retry limit is exhausted does Guardian pass the next explicitly
configured model to `turn/start`; it never passes an `effort` override. The
same active-Goal, idle-thread, source allowlist, process ownership, repeated
read, and deterministic message-ID checks still apply.

Configure the ordered fallback list on a `cli_turn` target or an allowlisted
`desktop_goal_state` target. An empty list keeps capacity recovery disabled:

```json
{
  "model_capacity_retry_limit": 10,
  "model_capacity_backoff_initial_seconds": 15,
  "model_capacity_backoff_max_seconds": 600,
  "model_capacity_fallback_models": ["gpt-5.6-terra"]
}
```

Index zero is always the thread's existing model, so the list contains only
lower-priority fallbacks. Retry counters, the selected model slot, and
`next_retry_at` are stored in Guardian state and survive watcher restarts.

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
proof. Sharing the desktop runtime removes the former split-brain Goal state,
while Guardian still makes Desktop wake an explicit allowlist opt-in, rejects
any active/in-progress task, performs repeated safety reads, and uses a
deterministic start ID.

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

The installer does not stop or restart Codex Desktop. After the current task is
safely idle, close and reopen the app once so its new main process reads
`CODEX_APP_SERVER_WS_URL`. That one restart is required when migrating from
0.5.x; later Desktop app updates continue to inherit the user-level setting.

If ChatGPT cannot open and its startup log shows `ECONNREFUSED` to the exact
Guardian `CODEX_APP_SERVER_WS_URL`, run `doctor` first. When the shared listener
is unavailable, use
`%LOCALAPPDATA%\CodexGoalGuardian\desktop-environment-backup.json` to restore
only the previous user environment value, then relaunch ChatGPT. This fail-open
repair preserves app access but disables Desktop shared-runtime recovery until
the listener is repaired and the Windows installer is rerun. It does not
require stopping the WSL or native CLI Guardian.

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
Hook. App updates do not change the shared App Server setting, external runtime,
or scheduled tasks.

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
  does not replace that remote Responses stream. After the failed turn settles,
  the local watcher reactivates and continues the Goal through the same App
  Server runtime used by the desktop renderer. CLI continuity starts after the
  original CLI process exits and network recovery is confirmed.
- Migrating from 0.5.x requires one safe Desktop app restart after installation.
  Until then, the already-running app still owns its old embedded App Server
  and Guardian deliberately reports the shared runtime as unavailable.
- With `delegated_continuity_enabled`, Guardian instructs the Goal to make all
  decisions already covered by its frozen objective and risk boundary without
  intermediate approval. New scope, missing user-only information, managed
  policy constraints, and ambiguous exact-once external effects are not
  silently invented or replayed.
- A platform `Invalid prompt` policy rejection is never replayed verbatim or
  treated as an older network failure. On an explicitly allowlisted Desktop
  target with `prompt_policy_retry_enabled`, Guardian may send one new fixed,
  policy-compliant continuation for that rejected turn. A second rejection is
  reported as `prompt_policy_retry_exhausted` and requires manual handling.
- Model-capacity recovery is opt-in per CLI or allowlisted Desktop target and
  requires at least one explicit fallback model. Guardian retries the thread's
  existing model first, preserves its current reasoning effort, and fails
  closed after every configured model exhausts its retry limit.
- An incompatible future App Server schema fails closed; it never falls back
  to direct database edits or UI automation.
- An explicit `desktop_thread_ids` entry opts that task into external wake.
  With delegated continuity enabled, Guardian repeatedly supervises each new
  terminal turn, resumes persisted state, and starts one deterministic
  continuation per evidence turn. The absence of an upstream
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
