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
    "CodexGoalGuardian-WSL",
    "CodexGoalGuardian-AppServer"
)
$InstallRoot = Join-Path $env:LOCALAPPDATA "CodexGoalGuardian"
$RuntimeRoot = Join-Path $InstallRoot "runtime"
$EnvironmentBackupPath = Join-Path $InstallRoot "desktop-environment-backup.json"

foreach ($TaskName in $TaskNames) {
    if ($DryRun) {
        Write-Host "[Codex Goal Guardian] dry-run: unregister $TaskName"
    } elseif (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
}

if (-not $DryRun -and (Test-Path -LiteralPath $EnvironmentBackupPath)) {
    $Backup = Get-Content -LiteralPath $EnvironmentBackupPath -Raw |
        ConvertFrom-Json
    if ($Backup.schema_version -ne 1 -or
        $Backup.variable -ne "CODEX_APP_SERVER_WS_URL") {
        throw "Unsupported Desktop environment backup at $EnvironmentBackupPath."
    }
    $RestoredValue = if ($Backup.present) { [string]$Backup.value } else { $null }
    [Environment]::SetEnvironmentVariable(
        "CODEX_APP_SERVER_WS_URL",
        $RestoredValue,
        [EnvironmentVariableTarget]::User
    )
    Remove-Item -LiteralPath $EnvironmentBackupPath -Force
    Write-Host "[Codex Goal Guardian] restored CODEX_APP_SERVER_WS_URL"
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
