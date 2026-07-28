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
    [string]$WslDistro = "Ubuntu-22.04",
    [string]$WslUser = "",
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
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$InstallRoot = Join-Path $env:LOCALAPPDATA "CodexGoalGuardian"
$RuntimeRoot = Join-Path $InstallRoot "runtime"
$ConfigPath = Join-Path $InstallRoot "config.json"
$StatePath = Join-Path $InstallRoot "state.json"
$LogPath = Join-Path $InstallRoot "guardian.jsonl"

if (-not $SkipWslTask -and [string]::IsNullOrWhiteSpace($WslUser)) {
    throw "Pass -WslUser with the native Linux user for $WslDistro."
}

function Write-Plan {
    param([string]$Message)
    Write-Host "[Codex Goal Guardian] $Message"
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
    param([string[]]$Command)

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
        [switch]$SkipWsl,
        [int]$TimeoutMinutes
    )

    $Deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $ClearObservations = 0
    $WaitingAnnounced = $false
    $ProbeFailureAnnounced = $false

    while ($true) {
        $WindowsActive = Test-WindowsRecoveryProcess -Command $WindowsCommand
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
            allowed_sources = @("cli", "exec")
            max_thread_age_seconds = 86400
            thread_limit = 50
            resume_grace_seconds = 2
            start_recovery_turn = $true
        }
    )
}

if ($DryRun) {
    Write-Plan "dry-run: copy runtime to $RuntimeRoot"
    Write-Plan "dry-run: write configuration to $ConfigPath"
    if (-not $SkipTasks) {
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
    -SkipWsl:$SkipWslTask -TimeoutMinutes $DrainTimeoutMinutes

foreach ($OwnedTask in @($TaskWatchdog, $TaskWindows, $TaskWsl)) {
    $ExistingTask = Get-ScheduledTask -TaskName $OwnedTask -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask -and $ExistingTask.State -eq "Running") {
        Stop-ScheduledTask -TaskName $OwnedTask
    }
}

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
    Write-Plan "preserved existing $ConfigPath (use -ForceConfig to replace)"
}

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

    Start-ScheduledTask -TaskName $TaskWindows
    if (-not $SkipWslTask) {
        Start-ScheduledTask -TaskName $TaskWsl
    }
    Start-ScheduledTask -TaskName $TaskWatchdog
}

Write-Plan "installation complete"
