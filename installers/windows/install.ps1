#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$ForceConfig,
    [switch]$SkipTasks,
    [switch]$SkipWslTask,
    [string]$CodexCommand = "",
    [string]$ProxyUrl = "",
    [string]$TcpHost = "",
    [int]$TcpPort = 0,
    [string[]]$DesktopThreadId = @(),
    [string]$WslDistro = "Ubuntu-22.04",
    [string]$WslUser = "",
    [ValidateRange(1024, 65535)]
    [int]$AppServerPort = 47831,
    [ValidateRange(5, 300)]
    [int]$WatchIntervalSeconds = 15,
    [ValidateRange(0, 1440)]
    [int]$DrainTimeoutMinutes = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskWindows = "CodexGoalGuardian-Windows"
$TaskWsl = "CodexGoalGuardian-WSL"
$TaskWatchdog = "CodexGoalGuardian-Watchdog"
$TaskAppServer = "CodexGoalGuardian-AppServer"
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$InstallRoot = Join-Path $env:LOCALAPPDATA "CodexGoalGuardian"
$RuntimeRoot = Join-Path $InstallRoot "runtime"
$ConfigPath = Join-Path $InstallRoot "config.json"
$StatePath = Join-Path $InstallRoot "state.json"
$LogPath = Join-Path $InstallRoot "guardian.jsonl"
$EnvironmentBackupPath = Join-Path $InstallRoot "desktop-environment-backup.json"
$SharedAppServerUrl = "ws://127.0.0.1:$AppServerPort/rpc"

if (-not $SkipWslTask -and [string]::IsNullOrWhiteSpace($WslUser)) {
    throw "Pass -WslUser with the native Linux user for $WslDistro."
}

$ReplaceDesktopThreadIds = $PSBoundParameters.ContainsKey("DesktopThreadId")
$DesktopThreadIds = @(
    foreach ($Value in $DesktopThreadId) {
        $Normalized = $Value.Trim()
        if (
            [string]::IsNullOrWhiteSpace($Normalized) -or
            $Normalized.Length -gt 128
        ) {
            throw "DesktopThreadId values must be non-empty and at most 128 characters."
        }
        $Normalized
    }
)
if (@($DesktopThreadIds | Select-Object -Unique).Count -ne $DesktopThreadIds.Count) {
    throw "DesktopThreadId values must be unique."
}

function Write-Plan {
    param([string]$Message)
    Write-Host "[Codex Goal Guardian] $Message"
}

function Set-JsonProperty {
    param(
        [object]$Object,
        [string]$Name,
        [object]$Value
    )

    if ($null -ne $Object.PSObject.Properties[$Name]) {
        $Object.$Name = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Update-GuardianConfig {
    param(
        [string]$Path,
        [System.Collections.IDictionary]$DesktopTarget,
        [bool]$ReplaceDesktopThreadIds
    )

    $Existing = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($Existing.schema_version -ne 1) {
        throw "Cannot migrate unsupported Guardian config schema at $Path."
    }
    $OriginalJson = $Existing | ConvertTo-Json -Depth 8
    $Targets = @($Existing.targets)
    $Matches = @(
        $Targets | Where-Object {
            $_.name -eq $DesktopTarget["name"]
        }
    )
    if ($Matches.Count -gt 1) {
        throw "Guardian config contains duplicate desktop goal-state targets."
    }
    if ($Matches.Count -eq 0) {
        $Existing.targets = @(
            $Targets + [pscustomobject]$DesktopTarget
        )
    } else {
        $Desktop = $Matches[0]
        Set-JsonProperty $Desktop "recovery_mode" "desktop_goal_state"
        Set-JsonProperty $Desktop "allowed_sources" @("vscode")
        Set-JsonProperty $Desktop "max_thread_age_seconds" 2592000
        Set-JsonProperty $Desktop "app_server_url" (
            $DesktopTarget["app_server_url"]
        )
        if ($ReplaceDesktopThreadIds) {
            Set-JsonProperty $Desktop "desktop_thread_ids" @(
                $DesktopTarget["desktop_thread_ids"]
            )
            Set-JsonProperty $Desktop "resume_grace_seconds" (
                $DesktopTarget["resume_grace_seconds"]
            )
            Set-JsonProperty $Desktop "start_recovery_turn" (
                $DesktopTarget["start_recovery_turn"]
            )
        }
    }

    $WindowsTargets = @(
        @($Existing.targets) | Where-Object {
            $_.name -eq "windows"
        }
    )
    if ($WindowsTargets.Count -gt 1) {
        throw "Guardian config contains duplicate Windows CLI targets."
    }
    if ($WindowsTargets.Count -eq 1) {
        $WindowsTarget = $WindowsTargets[0]
        $Sources = @()
        if ($null -ne $WindowsTarget.PSObject.Properties["allowed_sources"]) {
            $Sources = @($WindowsTarget.allowed_sources)
        }
        if (
            $Sources.Count -eq 1 -and
            $Sources[0] -eq "__disabled_until_guardian_upgrade__"
        ) {
            Set-JsonProperty $WindowsTarget "allowed_sources" @("cli", "exec")
        }
        if ($null -eq $WindowsTarget.PSObject.Properties["recovery_mode"]) {
            Set-JsonProperty $WindowsTarget "recovery_mode" "cli_turn"
        }
    }

    $UpdatedJson = $Existing | ConvertTo-Json -Depth 8
    if ($UpdatedJson -eq $OriginalJson) {
        Write-Plan "preserved existing settings in $Path"
        return
    }

    $BackupPath = "$Path.pre-0.3.0.bak"
    if (-not (Test-Path -LiteralPath $BackupPath)) {
        Copy-Item -LiteralPath $Path -Destination $BackupPath
    }
    $TemporaryPath = "$Path.tmp.$PID"
    try {
        $UpdatedJson |
            Set-Content -LiteralPath $TemporaryPath -Encoding UTF8
        $null = Get-Content -LiteralPath $TemporaryPath -Raw |
            ConvertFrom-Json
        Move-Item -LiteralPath $TemporaryPath -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $TemporaryPath) {
            Remove-Item -LiteralPath $TemporaryPath -Force
        }
    }
    Write-Plan "migrated update-safe desktop Goal recovery in $Path"
}

function Publish-EnvironmentChange {
    if ($null -eq ("GuardianNativeMethods" -as [type])) {
        Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class GuardianNativeMethods {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr SendMessageTimeout(
        IntPtr hWnd,
        uint Msg,
        UIntPtr wParam,
        string lParam,
        uint fuFlags,
        uint uTimeout,
        out UIntPtr lpdwResult
    );
}
'@
    }
    $Result = [UIntPtr]::Zero
    $null = [GuardianNativeMethods]::SendMessageTimeout(
        [IntPtr]0xffff,
        0x001A,
        [UIntPtr]::Zero,
        "Environment",
        0x0002,
        5000,
        [ref]$Result
    )
}

function Set-DesktopAppServerEnvironment {
    param(
        [string]$BackupPath,
        [string]$Url
    )

    $ForceCli = [Environment]::GetEnvironmentVariable(
        "CODEX_APP_SERVER_FORCE_CLI",
        [EnvironmentVariableTarget]::User
    )
    if ($ForceCli -eq "1") {
        throw (
            "CODEX_APP_SERVER_FORCE_CLI=1 disables the Desktop WebSocket " +
            "transport. Remove that user environment override first."
        )
    }
    if (-not (Test-Path -LiteralPath $BackupPath)) {
        $Previous = [Environment]::GetEnvironmentVariable(
            "CODEX_APP_SERVER_WS_URL",
            [EnvironmentVariableTarget]::User
        )
        $Backup = [ordered]@{
            schema_version = 1
            variable = "CODEX_APP_SERVER_WS_URL"
            present = $null -ne $Previous
            value = $Previous
        }
        $Backup | ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath $BackupPath -Encoding UTF8
    }
    [Environment]::SetEnvironmentVariable(
        "CODEX_APP_SERVER_WS_URL",
        $Url,
        [EnvironmentVariableTarget]::User
    )
    Publish-EnvironmentChange
    Write-Plan "configured Desktop shared AppServer at $Url"
}

function Resolve-NativeCodex {
    if ($CodexCommand) {
        if (Test-Path -LiteralPath $CodexCommand -PathType Leaf) {
            return (Resolve-Path -LiteralPath $CodexCommand).Path
        }
        $Explicit = Get-Command $CodexCommand -ErrorAction Stop
        return $Explicit.Source
    }
    $Application = Get-Command "codex.cmd" -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $Application) {
        throw "Native codex.cmd was not found. Install @openai/codex or pass -CodexCommand."
    }
    return $Application.Source
}

function Resolve-NativePython {
    $PyLauncher = Get-Command "py.exe" -CommandType Application `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $PyLauncher) {
        $Resolved = (& $PyLauncher.Source -3 -c "import sys; print(sys.executable)" | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $Resolved).Path
        }
    }
    $Python = Get-Command "python.exe" -CommandType Application `
        -ErrorAction Stop | Select-Object -First 1
    return $Python.Source
}

function Test-WindowsRecoveryProcess {
    param(
        [string[]]$Command,
        [string]$SharedUrl
    )

    foreach ($Process in @(Get-CimInstance Win32_Process)) {
        if ($Process.ProcessId -eq $PID -or
            [string]::IsNullOrWhiteSpace($Process.CommandLine)) {
            continue
        }
        if ($Process.CommandLine.IndexOf(
                "app-server",
                [StringComparison]::OrdinalIgnoreCase
            ) -lt 0) {
            continue
        }
        if (
            -not [string]::IsNullOrWhiteSpace($SharedUrl) -and
            $Process.CommandLine.IndexOf(
                $SharedUrl,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        ) {
            continue
        }

        $MatchesCommand = $true
        foreach ($CommandPart in $Command) {
            if ($Process.CommandLine.IndexOf(
                    $CommandPart,
                    [StringComparison]::OrdinalIgnoreCase
                ) -lt 0) {
                $MatchesCommand = $false
                break
            }
        }
        if ($MatchesCommand) {
            return $true
        }
    }
    return $false
}

function Test-WslRecoveryProcess {
    param(
        [string]$Distro,
        [string]$LinuxUser
    )

    $Wsl = Get-Command "wsl.exe" -CommandType Application `
        -ErrorAction Stop | Select-Object -First 1
    $Probe = @'
current=$$
for path in /proc/[0-9]*/cmdline; do
    pid=${path#/proc/}
    pid=${pid%/cmdline}
    [ "$pid" = "$current" ] && continue
    [ -r "$path" ] || continue
    line=$(tr '\000' ' ' < "$path" 2>/dev/null)
    case "$line" in
        *codex*app-server*) exit 0 ;;
    esac
done
exit 1
'@

    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $null = & $Wsl.Source -d $Distro --user $LinuxUser `
            --exec sh -c $Probe 2>$null
        $ProbeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ProbeExitCode -eq 0) {
        return $true
    }
    if ($ProbeExitCode -eq 1) {
        return $false
    }
    throw "Unable to inspect active Codex recovery processes in $Distro (exit $ProbeExitCode)."
}

function Wait-GuardianRecoveryDrain {
    param(
        [string[]]$WindowsCommand,
        [string]$Distro,
        [string]$LinuxUser,
        [string]$SharedUrl,
        [switch]$SkipWsl,
        [int]$TimeoutMinutes
    )

    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $ClearObservations = 0
    $WaitingAnnounced = $false
    $ProbeFailureAnnounced = $false

    while ($true) {
        $WindowsActive = Test-WindowsRecoveryProcess `
            -Command $WindowsCommand -SharedUrl $SharedUrl
        $WslActive = $false
        if (-not $SkipWsl) {
            try {
                $WslActive = Test-WslRecoveryProcess `
                    -Distro $Distro -LinuxUser $LinuxUser
            } catch {
                $WslActive = $true
                if (-not $ProbeFailureAnnounced) {
                    Write-Warning (
                        "$($_.Exception.Message) Treating the probe as active " +
                        "and continuing to wait without stopping any task."
                    )
                    $ProbeFailureAnnounced = $true
                }
            }
        }

        if (-not $WindowsActive -and -not $WslActive) {
            $ClearObservations += 1
            if ($ClearObservations -ge 2) {
                Write-Plan "recovery processes are drained; installation may continue"
                return
            }
        } else {
            $ClearObservations = 0
            if (-not $WaitingAnnounced) {
                Write-Plan "active recovery detected; waiting for it to finish naturally"
                $WaitingAnnounced = $true
            }
            if ($TimeoutMinutes -eq 0) {
                throw "Active Codex recovery detected. No tasks were stopped. Re-run with -DrainTimeoutMinutes <minutes> to wait for a safe update window."
            }
        }

        if ($TimeoutMinutes -gt 0 -and (Get-Date) -ge $Deadline) {
            throw "Timed out waiting for active Codex recovery processes. No tasks were stopped."
        }
        Start-Sleep -Seconds 1
    }
}

function Stop-DetachedGuardianWatchers {
    param(
        [string]$Runtime,
        [string]$Distro,
        [string]$LinuxUser,
        [switch]$SkipWsl
    )

    $Launcher = Join-Path $Runtime "scripts\guardian-launch.py"
    foreach ($Process in @(Get-CimInstance Win32_Process)) {
        if (
            $Process.ProcessId -eq $PID -or
            [string]::IsNullOrWhiteSpace($Process.CommandLine) -or
            $Process.CommandLine.IndexOf(
                $Launcher,
                [StringComparison]::OrdinalIgnoreCase
            ) -lt 0 -or
            $Process.CommandLine.IndexOf(
                " watch --config ",
                [StringComparison]::OrdinalIgnoreCase
            ) -lt 0
        ) {
            continue
        }
        Stop-Process -Id $Process.ProcessId -ErrorAction Stop
        Write-Plan "stopped detached Guardian Windows watcher $($Process.ProcessId)"
    }

    if ($SkipWsl) {
        return
    }
    $Wsl = Get-Command "wsl.exe" -CommandType Application `
        -ErrorAction Stop | Select-Object -First 1
    $WslConfig = "/home/$LinuxUser/.config/codex-goal-guardian/config.json"
    $Terminate = @'
current=$$
for path in /proc/[0-9]*/cmdline; do
    pid=${path#/proc/}
    pid=${pid%/cmdline}
    [ "$pid" = "$current" ] && continue
    [ -r "$path" ] || continue
    line=$(tr '\000' ' ' < "$path" 2>/dev/null)
    case "$line" in
        *python3*-m*codex_goal_guardian*watch*--config*__CONFIG__*)
            kill -TERM "$pid"
            ;;
    esac
done
'@.Replace("__CONFIG__", $WslConfig)
    & $Wsl.Source -d $Distro --user $LinuxUser `
        --exec sh -c $Terminate
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to stop the detached Guardian WSL watcher."
    }

    $WslMarker = (
        "/codex-goal-guardian/bin/codex-goal-guardian watch " +
        "--config $WslConfig"
    )
    foreach ($Attempt in 1..10) {
        $Remaining = @(
            Get-CimInstance Win32_Process | Where-Object {
                -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and (
                    $_.CommandLine.IndexOf(
                        $Launcher,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0 -or
                    $_.CommandLine.IndexOf(
                        $WslMarker,
                        [StringComparison]::OrdinalIgnoreCase
                    ) -ge 0
                )
            }
        )
        if ($Remaining.Count -eq 0) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Detached Guardian watcher did not exit after a graceful stop."
}

$CodexPath = Resolve-NativeCodex
$PythonPath = Resolve-NativePython
$PythonwPath = Join-Path (Split-Path -Parent $PythonPath) "pythonw.exe"
if (-not (Test-Path -LiteralPath $PythonwPath -PathType Leaf)) {
    throw "Native pythonw.exe was not found beside $PythonPath."
}
$PythonwPath = (Resolve-Path -LiteralPath $PythonwPath).Path
$GuardianCommand = @($CodexPath)
if ([IO.Path]::GetExtension($CodexPath) -ieq ".cmd") {
    $CodexJs = Join-Path (Split-Path -Parent $CodexPath) "node_modules\@openai\codex\bin\codex.js"
    $Node = Get-Command "node.exe" -CommandType Application -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $CodexJs -PathType Leaf)) {
        throw "Unable to resolve the JavaScript entrypoint beside $CodexPath."
    }
    $GuardianCommand = @($Node.Source, (Resolve-Path -LiteralPath $CodexJs).Path)
}
$GuardianExecutable = $GuardianCommand[0]
$GuardianBaseArguments = @($GuardianCommand | Select-Object -Skip 1)
$VersionOutput = (& $GuardianExecutable @GuardianBaseArguments --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Codex version check failed: $VersionOutput"
}
$AppServerOutput = (& $GuardianExecutable @GuardianBaseArguments app-server --help 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Codex app-server is unavailable: $AppServerOutput"
}
Write-Plan "verified $VersionOutput via $GuardianExecutable"
Write-Plan "verified native Python via $PythonPath"
Write-Plan "verified windowless Python via $PythonwPath"

$ProxyValue = if ($ProxyUrl) { $ProxyUrl } else { $null }
$TcpHostValue = if ($TcpHost) { $TcpHost } else { $null }
$TcpPortValue = if ($TcpPort -gt 0) { $TcpPort } else { $null }
$DesktopWakeEnabled = $DesktopThreadIds.Count -gt 0
$DesktopGoalStateTarget = [ordered]@{
    name = "windows-desktop-goal-state"
    command = @($GuardianCommand)
    codex_home = (Join-Path $env:USERPROFILE ".codex")
    app_server_url = $SharedAppServerUrl
    recovery_mode = "desktop_goal_state"
    allowed_sources = @("vscode")
    max_thread_age_seconds = 2592000
    thread_limit = 50
    resume_grace_seconds = if ($DesktopWakeEnabled) { 2 } else { 0 }
    start_recovery_turn = $DesktopWakeEnabled
    desktop_thread_ids = @($DesktopThreadIds)
}
$Configuration = [ordered]@{
    schema_version = 1
    state_path = $StatePath
    log_path = $LogPath
    health = [ordered]@{
        url = "https://chatgpt.com/backend-api/codex"
        proxy_url = $ProxyValue
        tcp_host = $TcpHostValue
        tcp_port = $TcpPortValue
        timeout_seconds = 8
        required_consecutive_successes = 2
        required_consecutive_failures = 2
    }
    targets = @(
        [ordered]@{
            name = "windows"
            command = @($GuardianCommand)
            codex_home = (Join-Path $env:USERPROFILE ".codex")
            recovery_mode = "cli_turn"
            allowed_sources = @("cli", "exec")
            max_thread_age_seconds = 86400
            thread_limit = 50
            resume_grace_seconds = 2
            start_recovery_turn = $true
        },
        $DesktopGoalStateTarget
    )
}

if ($DryRun) {
    Write-Plan "dry-run: copy runtime to $RuntimeRoot"
    Write-Plan "dry-run: write configuration to $ConfigPath"
    Write-Plan "dry-run: set CODEX_APP_SERVER_WS_URL=$SharedAppServerUrl"
    if (-not $SkipTasks) {
        Write-Plan "dry-run: register $TaskAppServer"
        Write-Plan "dry-run: register $TaskWindows"
        if (-not $SkipWslTask) {
            Write-Plan "dry-run: register $TaskWsl for $WslDistro"
        }
        Write-Plan "dry-run: register $TaskWatchdog"
    }
    exit 0
}

Wait-GuardianRecoveryDrain -WindowsCommand $GuardianCommand `
    -Distro $WslDistro -LinuxUser $WslUser `
    -SharedUrl $SharedAppServerUrl `
    -SkipWsl:$SkipWslTask -TimeoutMinutes $DrainTimeoutMinutes

foreach ($OwnedTask in @($TaskWatchdog, $TaskWindows, $TaskWsl)) {
    $ExistingTask = Get-ScheduledTask -TaskName $OwnedTask -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask -and $ExistingTask.State -eq "Running") {
        Stop-ScheduledTask -TaskName $OwnedTask
    }
}
Stop-DetachedGuardianWatchers -Runtime $RuntimeRoot `
    -Distro $WslDistro -LinuxUser $WslUser -SkipWsl:$SkipWslTask

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
if (Test-Path -LiteralPath $RuntimeRoot) {
    Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceRoot "src") -Destination $RuntimeRoot -Recurse
Copy-Item -LiteralPath (Join-Path $SourceRoot "scripts") -Destination $RuntimeRoot -Recurse
Copy-Item -LiteralPath (Join-Path $SourceRoot "pyproject.toml") -Destination $RuntimeRoot

if ($ForceConfig -or -not (Test-Path -LiteralPath $ConfigPath)) {
    $Configuration | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
    Write-Plan "wrote $ConfigPath"
} else {
    Update-GuardianConfig -Path $ConfigPath `
        -DesktopTarget $DesktopGoalStateTarget `
        -ReplaceDesktopThreadIds $ReplaceDesktopThreadIds
}
Set-DesktopAppServerEnvironment `
    -BackupPath $EnvironmentBackupPath -Url $SharedAppServerUrl

if (-not $SkipTasks) {
    $PowerShellPath = (Get-Command "powershell.exe" -ErrorAction Stop).Source
    $LauncherPath = Join-Path $RuntimeRoot "scripts\guardian-launch.py"
    $NativeArguments = "`"$LauncherPath`" watch --config `"$ConfigPath`" --interval $WatchIntervalSeconds --json"
    $NativeAction = New-ScheduledTaskAction -Execute $PythonwPath `
        -Argument $NativeArguments -WorkingDirectory $RuntimeRoot
    $LogonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $IntervalTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1)
    $WatcherSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

    $QuoteArgument = {
        param([string]$Value)
        '"' + $Value.Replace('"', '\"') + '"'
    }
    $AppServerChildArguments = @(
        $GuardianExecutable
    ) + @($GuardianBaseArguments) + @(
        "app-server",
        "--listen",
        $SharedAppServerUrl
    )
    $AppServerArguments = (
        (& $QuoteArgument $LauncherPath) +
        " --windows-hidden-child " +
        (
            @(
                $AppServerChildArguments | ForEach-Object {
                    & $QuoteArgument $_
                }
            ) -join " "
        )
    )
    $AppServerAction = New-ScheduledTaskAction -Execute $PythonwPath `
        -Argument $AppServerArguments -WorkingDirectory $RuntimeRoot
    Register-ScheduledTask -TaskName $TaskAppServer `
        -Action $AppServerAction -Trigger $LogonTrigger `
        -Settings $WatcherSettings `
        -Description "Host the shared loopback AppServer used by Codex Desktop and Goal Guardian." `
        -Force | Out-Null
    Write-Plan "registered $TaskAppServer"

    Register-ScheduledTask -TaskName $TaskWindows -Action $NativeAction `
        -Trigger $LogonTrigger -Settings $WatcherSettings `
        -Description "Resume eligible active Codex Goals after network recovery." `
        -Force | Out-Null
    Write-Plan "registered $TaskWindows"

    if (-not $SkipWslTask) {
        $WslPath = (Get-Command "wsl.exe" -ErrorAction Stop).Source
        $WslLauncher = "/home/$WslUser/.local/share/codex-goal-guardian/bin/codex-goal-guardian"
        $WslConfig = "/home/$WslUser/.config/codex-goal-guardian/config.json"
        $WslArguments = "`"$LauncherPath`" --windows-hidden-child `"$WslPath`" -d $WslDistro --user $WslUser --exec $WslLauncher watch --config $WslConfig --interval $WatchIntervalSeconds --json"
        $WslAction = New-ScheduledTaskAction -Execute $PythonwPath `
            -Argument $WslArguments -WorkingDirectory $RuntimeRoot
        Register-ScheduledTask -TaskName $TaskWsl -Action $WslAction `
            -Trigger $LogonTrigger -Settings $WatcherSettings `
            -Description "Resume eligible active WSL Codex Goals after network recovery." `
            -Force | Out-Null
        Write-Plan "registered $TaskWsl"
    }

    $WatchdogPath = Join-Path $RuntimeRoot "scripts\watchdog-windows.ps1"
    $WatchdogArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$WatchdogPath`""
    if ($SkipWslTask) {
        $WatchdogArguments += " -SkipWsl"
    }
    $HiddenWatchdogArguments = "`"$LauncherPath`" --windows-hidden-child `"$PowerShellPath`" $WatchdogArguments"
    $WatchdogAction = New-ScheduledTaskAction -Execute $PythonwPath `
        -Argument $HiddenWatchdogArguments -WorkingDirectory $RuntimeRoot
    $WatchdogSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskWatchdog -Action $WatchdogAction `
        -Trigger @($LogonTrigger, $IntervalTrigger) -Settings $WatchdogSettings `
        -Description "Restart Codex Goal Guardian watchers when they exit." `
        -Force | Out-Null
    Write-Plan "registered $TaskWatchdog"

    Start-ScheduledTask -TaskName $TaskAppServer
    $ListenerReady = $false
    foreach ($Attempt in 1..50) {
        $Listeners = @(
            Get-NetTCPConnection -LocalAddress "127.0.0.1" `
                -LocalPort $AppServerPort -State Listen `
                -ErrorAction SilentlyContinue
        )
        foreach ($Listener in $Listeners) {
            $Owner = Get-CimInstance Win32_Process `
                -Filter "ProcessId=$($Listener.OwningProcess)"
            if (
                $null -ne $Owner -and
                -not [string]::IsNullOrWhiteSpace($Owner.CommandLine) -and
                $Owner.CommandLine.IndexOf(
                    "app-server",
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0 -and
                $Owner.CommandLine.IndexOf(
                    $SharedAppServerUrl,
                    [StringComparison]::OrdinalIgnoreCase
                ) -ge 0
            ) {
                $ListenerReady = $true
                break
            }
        }
        if ($ListenerReady) {
            break
        }
        if ($Listeners.Count -gt 0) {
            throw (
                "Port $AppServerPort is owned by a different process; " +
                "shared AppServer was not started."
            )
        }
        Start-Sleep -Milliseconds 100
    }
    if (-not $ListenerReady) {
        throw "Shared AppServer did not listen at $SharedAppServerUrl."
    }
    Start-ScheduledTask -TaskName $TaskWindows
    if (-not $SkipWslTask) {
        Start-ScheduledTask -TaskName $TaskWsl
    }
    Start-ScheduledTask -TaskName $TaskWatchdog
}

Write-Plan "installation complete"
