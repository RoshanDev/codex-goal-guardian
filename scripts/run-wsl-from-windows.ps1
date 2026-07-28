[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu-22.04",
    [string]$WslUser = "roshan",
    [ValidateRange(5, 300)]
    [int]$IntervalSeconds = 15
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RuntimeRoot = Split-Path -Parent $PSScriptRoot
$InstallRoot = Split-Path -Parent $RuntimeRoot
$LogPath = Join-Path $InstallRoot "wsl-task.jsonl"
$Wsl = (Get-Command "wsl.exe" -ErrorAction Stop).Source
$Launcher = "/home/$WslUser/.local/share/codex-goal-guardian/bin/codex-goal-guardian"
$Config = "/home/$WslUser/.config/codex-goal-guardian/config.json"

# WSL can emit a harmless NAT/localhost warning on stderr while returning 0.
$PreviousErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    & $Wsl -d $Distro --user $WslUser --exec $Launcher `
        watch --config $Config --interval $IntervalSeconds --json `
        1>$null 2>$null
    $ExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $PreviousErrorPreference
}
if ($ExitCode -ne 0) {
    $Record = [ordered]@{
        timestamp = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        exit_code = $ExitCode
    }
    $Record | ConvertTo-Json -Compress | Add-Content -LiteralPath $LogPath -Encoding UTF8
}
exit $ExitCode
