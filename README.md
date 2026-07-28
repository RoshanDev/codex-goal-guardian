# Codex Goal Guardian

Codex Goal Guardian is an external recovery supervisor for active Codex Goals
that stop after the built-in reconnect limit. It covers:

- the Windows ChatGPT/Codex desktop app and native Windows Codex CLI, which
  share the Windows Codex session home;
- Codex CLI running natively inside WSL2 Ubuntu 22.04.

The supervisor lives in stable user directories and uses the documented Codex
App Server JSON-RPC surface. Desktop app updates do not overwrite it.

## Why this survives updates

The Windows app package, `app.asar`, private UI bridges, and versioned editor
extensions are outside the trust boundary. Windows Task Scheduler launches
15-second native Windows and WSL watchers; an optional WSL user timer is a
fallback. A one-minute watchdog restarts either watcher if it exits. All three
tasks use a standalone Python runtime copied to:

- Windows: `%LOCALAPPDATA%\CodexGoalGuardian`
- WSL2: `~/.local/share/codex-goal-guardian`

The bundled Codex plugin adds diagnostics and privacy-filtered Stop evidence;
it is not the long-running authority. This avoids the repair-after-every-update
behavior of main-process patchers such as Codex++.

## Recovery contract

Guardian requires two consecutive failed probes before declaring an outage,
then two consecutive healthy probes before recovery. A single TLS/proxy blip
does not create an outage generation. It then considers only recent threads
with:

- a persisted Goal whose status is `active`;
- thread status `idle`, `systemError`, or `notLoaded`;
- a final turn that is no longer `inProgress`;
- no completed action for the same target, outage generation, and thread.

Immediately before mutation it reads the thread and Goal again. It calls
`thread/resume`, waits briefly, and starts a deterministic reconciliation turn
only if the thread is still idle. If the server-side turn remains active, the
same App Server connection stays open and is checked until that turn settles;
closing the connection early would interrupt the resumed turn. Successful
stages are persisted atomically. Each fresh App Server process reopens the
thread with `thread/resume` before `turn/start`, and the recovery turn remains
attached until completion. It uses a deterministic client message ID so retries
remain idempotent.

The reconciliation prompt tells Codex to inspect recorded terminal state and
avoid repeating successful commands or mutations.

## Requirements

- Python 3.10 or newer on each native runtime
- Codex CLI with `codex app-server`
- Windows PowerShell 5.1 or newer
- WSL2 Ubuntu 22.04 with a native Linux Codex executable

Do not install against a WSL command that resolves under `/mnt/c`. The WSL
installer rejects Windows shims explicitly.

## Install

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
`plugin/codex-goal-guardian`. Its Skill guides safe diagnosis, and its Stop
hook delegates to the external installation only when found. The hook has a
two-second subprocess timeout, ignores prompt and tool payloads, and always
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
  does not replace or reattach that UI transport stream; after the stream is
  exhausted, it resumes the persisted thread and starts a new reconciliation
  turn when doing so is safe.
- Guardian cannot continue work that is waiting for user input or approval.
- An incompatible future App Server schema fails closed; it never falls back
  to direct database edits or UI automation.
- A desktop-only internal turn that has not been persisted through the shared
  Windows Codex home cannot be reconstructed.
- Health restoration must be observed by the scheduler. Installing while the
  network is already healthy does not synthesize an outage.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See `docs/plans/` for the design and implementation plan.
