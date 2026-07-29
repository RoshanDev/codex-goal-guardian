[CmdletBinding()]
param(
    [switch]$SkipWsl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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
