#requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$PurgeData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskNames = @(
    "CodexGoalGuardian-Watchdog",
    "CodexGoalGuardian-Windows",
    "CodexGoalGuardian-WSL"
)
$InstallRoot = Join-Path $env:LOCALAPPDATA "CodexGoalGuardian"
$RuntimeRoot = Join-Path $InstallRoot "runtime"

foreach ($TaskName in $TaskNames) {
    if ($DryRun) {
        Write-Host "[Codex Goal Guardian] dry-run: unregister $TaskName"
    } elseif (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}

if ($DryRun) {
    Write-Host "[Codex Goal Guardian] dry-run: remove $RuntimeRoot"
    if ($PurgeData) {
        Write-Host "[Codex Goal Guardian] dry-run: purge $InstallRoot"
    }
    exit 0
}

if (Test-Path -LiteralPath $RuntimeRoot) {
    Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
}
if ($PurgeData -and (Test-Path -LiteralPath $InstallRoot)) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
} else {
    Write-Host "[Codex Goal Guardian] preserved configuration, state, and logs in $InstallRoot"
}
