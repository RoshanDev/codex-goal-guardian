[CmdletBinding()]
param(
    [switch]$SkipWsl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$InstallRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$MaintenancePath = Join-Path $InstallRoot "maintenance.lock"
if (Test-Path -LiteralPath $MaintenancePath) {
    $MaintenancePid = 0
    $RawPid = (
        Get-Content -LiteralPath $MaintenancePath -TotalCount 1
    ).Trim()
    if (
        [int]::TryParse($RawPid, [ref]$MaintenancePid) -and
        $null -ne (
            Get-Process -Id $MaintenancePid -ErrorAction SilentlyContinue
        )
    ) {
        exit 0
    }
    Remove-Item -LiteralPath $MaintenancePath -Force `
        -ErrorAction SilentlyContinue
}

$TaskNames = @(
    "CodexGoalGuardian-AppServer",
    "CodexGoalGuardian-Windows"
)
if (-not $SkipWsl) {
    $TaskNames += "CodexGoalGuardian-WSL"
}
foreach ($TaskName in $TaskNames) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($Task.State -ne "Running") {
        Start-ScheduledTask -TaskName $TaskName
    }
}
