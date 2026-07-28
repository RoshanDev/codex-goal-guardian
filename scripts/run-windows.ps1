[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$GuardianArgs
)

$ErrorActionPreference = "Stop"
$RuntimeRoot = Split-Path -Parent $PSScriptRoot
$InstallRoot = Split-Path -Parent $RuntimeRoot
$ConfigPath = Join-Path $InstallRoot "config.json"
$env:PYTHONPATH = Join-Path $RuntimeRoot "src"

if (-not $GuardianArgs -or $GuardianArgs.Count -eq 0) {
    $GuardianArgs = @("run-once", "--config", $ConfigPath, "--json")
}

$Python = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($null -ne $Python) {
    $PythonPath = (& $Python.Source -3 -c "import sys; print(sys.executable)" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Unable to resolve the native Python 3 executable."
    }
    & $PythonPath -m codex_goal_guardian @GuardianArgs
} else {
    $Python = Get-Command "python.exe" -ErrorAction Stop
    & $Python.Source -m codex_goal_guardian @GuardianArgs
}
exit $LASTEXITCODE
