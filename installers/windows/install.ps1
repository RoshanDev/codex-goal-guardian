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
    [string]$WslUser = "roshan",
    [ValidateRange(5, 300)]
    [int]$WatchIntervalSeconds = 15
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

$CodexPath = Resolve-NativeCodex
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
    $RunnerPath = Join-Path $RuntimeRoot "scripts\run-windows.ps1"
    $NativeArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$RunnerPath`" watch --config `"$ConfigPath`" --interval $WatchIntervalSeconds --json"
    $NativeAction = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $NativeArguments
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
        $WslBridgePath = Join-Path $RuntimeRoot "scripts\run-wsl-from-windows.ps1"
        $WslArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$WslBridgePath`" -Distro `"$WslDistro`" -WslUser `"$WslUser`" -IntervalSeconds $WatchIntervalSeconds"
        $WslAction = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $WslArguments
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
    $WatchdogAction = New-ScheduledTaskAction -Execute $PowerShellPath -Argument $WatchdogArguments
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
