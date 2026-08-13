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

$AppServerTaskName = "CodexGoalGuardian-AppServer"
$DesktopConfigPath = Join-Path $InstallRoot "config-desktop.json"
if (-not (Test-Path -LiteralPath $DesktopConfigPath)) {
    $DesktopConfigPath = Join-Path $InstallRoot "config.json"
}
$DesktopConfig = Get-Content -LiteralPath $DesktopConfigPath -Raw |
    ConvertFrom-Json
$DesktopTarget = @(
    @($DesktopConfig.targets) | Where-Object {
        $_.recovery_mode -eq "desktop_goal_state"
    }
) | Select-Object -First 1
if ($null -eq $DesktopTarget) {
    throw "Guardian Desktop target is missing from $DesktopConfigPath."
}
$AppServerRpcUri = [Uri]$DesktopTarget.app_server_url
$AppServerPort = $AppServerRpcUri.Port
$AppServerListenUrl = $DesktopTarget.app_server_url -replace '/rpc$', ''

function Get-GuardianAppServerProcesses {
    $Processes = @(
        Get-CimInstance Win32_Process | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
            $_.CommandLine.IndexOf(
                "app-server",
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0 -and
            $_.CommandLine.IndexOf(
                $AppServerListenUrl,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        }
    )
    return @($Processes)
}

function Test-GuardianAppServerListener {
    $Listeners = @(
        Get-NetTCPConnection -LocalAddress "127.0.0.1" `
            -LocalPort $AppServerPort -State Listen `
            -ErrorAction SilentlyContinue
    )
    if ($Listeners.Count -eq 0) {
        return $false
    }
    $OwnedProcessIds = @(
        Get-GuardianAppServerProcesses | ForEach-Object {
            [int]$_.ProcessId
        }
    )
    foreach ($Listener in $Listeners) {
        if ($OwnedProcessIds -contains [int]$Listener.OwningProcess) {
            return $true
        }
    }
    throw (
        "Port $AppServerPort is listening but is not owned by the " +
        "configured Guardian AppServer."
    )
}

function Stop-GuardianAppServer {
    Stop-ScheduledTask -TaskName $AppServerTaskName `
        -ErrorAction SilentlyContinue
    $Owned = @(Get-GuardianAppServerProcesses)
    foreach ($Process in @($Owned | Sort-Object ProcessId -Descending)) {
        Stop-Process -Id $Process.ProcessId -Force `
            -ErrorAction SilentlyContinue
    }
    foreach ($Attempt in 1..50) {
        if (-not (Test-GuardianAppServerListener)) {
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Guardian AppServer listener did not stop for requested recovery."
}

function Start-GuardianAppServer {
    if (Test-GuardianAppServerListener) {
        return
    }
    $Task = Get-ScheduledTask -TaskName $AppServerTaskName -ErrorAction Stop
    if ($Task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $AppServerTaskName
        Start-Sleep -Milliseconds 250
    }
    Start-ScheduledTask -TaskName $AppServerTaskName
    foreach ($Attempt in 1..100) {
        if (Test-GuardianAppServerListener) {
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "Guardian AppServer did not listen after requested recovery."
}

function Test-GuardianWslWatcher {
    $Task = Get-ScheduledTask -TaskName "CodexGoalGuardian-WSL" `
        -ErrorAction Stop
    $Arguments = [string]$Task.Actions[0].Arguments
    $DistroMatch = [regex]::Match(
        $Arguments,
        '(?:^|\s)-d\s+(?:"([^"]+)"|(\S+))'
    )
    $UserMatch = [regex]::Match(
        $Arguments,
        '(?:^|\s)--user\s+(?:"([^"]+)"|(\S+))'
    )
    if (-not $DistroMatch.Success -or -not $UserMatch.Success) {
        return $Task.State -eq "Running"
    }
    $Distro = if ($DistroMatch.Groups[1].Success) {
        $DistroMatch.Groups[1].Value
    } else {
        $DistroMatch.Groups[2].Value
    }
    $LinuxUser = if ($UserMatch.Groups[1].Success) {
        $UserMatch.Groups[1].Value
    } else {
        $UserMatch.Groups[2].Value
    }
    $Config = "/home/$LinuxUser/.config/codex-goal-guardian/config.json"
    $Probe = @'
current=$$
for path in /proc/[0-9]*/cmdline; do
    pid=${path#/proc/}
    pid=${pid%/cmdline}
    [ "$pid" = "$current" ] && continue
    [ -r "$path" ] || continue
    line=$(tr '\000' ' ' < "$path" 2>/dev/null)
    case "$line" in
        *python3*-m*codex_goal_guardian*watch*--config*__CONFIG__*) exit 0 ;;
    esac
done
exit 1
'@.Replace("__CONFIG__", $Config)
    $Wsl = Get-Command "wsl.exe" -CommandType Application `
        -ErrorAction Stop | Select-Object -First 1
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
    # An interop failure is not proof that the Linux watcher exited. Avoid
    # starting another instance; the next watchdog pass will probe again.
    return $true
}

$RestartRequestPath = Join-Path $InstallRoot "app-server-restart.request"
if (Test-Path -LiteralPath $RestartRequestPath) {
    Stop-GuardianAppServer
    Start-GuardianAppServer
    Move-Item -LiteralPath $RestartRequestPath -Destination (
        Join-Path $InstallRoot "app-server-restart.last.json"
    ) -Force
} else {
    Start-GuardianAppServer
}

$TaskNames = @(
    "CodexGoalGuardian-Desktop",
    "CodexGoalGuardian-Windows"
)
if (-not $SkipWsl) {
    $TaskNames += "CodexGoalGuardian-WSL"
}
foreach ($TaskName in $TaskNames) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($TaskName -eq "CodexGoalGuardian-WSL") {
        if (Test-GuardianWslWatcher) {
            continue
        }
        if ($Task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $TaskName
            Start-Sleep -Milliseconds 250
        }
        Start-ScheduledTask -TaskName $TaskName
        continue
    }
    if ($Task.State -ne "Running") {
        Start-ScheduledTask -TaskName $TaskName
    }
}
