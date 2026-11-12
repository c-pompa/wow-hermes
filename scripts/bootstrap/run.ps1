#Requires -Version 5.1
<#
.SYNOPSIS
    Start the WoWHermes bridge from a per-user install.

.DESCRIPTION
    Default: run in this console so errors are visible.
    -Background: start a detached, hidden process and return.
    -Log: redirect stdout/stderr into <InstallDir>\logs\ (used by the scheduled task;
          the process stays in the foreground so Task Scheduler tracks its lifetime).

    Only the wowhermes-bridge process is started -- this script never starts, stops or
    touches the World of Warcraft client.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'wowhermes'),
    [switch]$Background,
    [switch]$Log
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$exe = Join-Path $InstallDir 'venv\Scripts\wowhermes-bridge.exe'
$logs = Join-Path $InstallDir 'logs'

if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "error: the bridge is not installed at $InstallDir" -ForegroundColor Red
    Write-Host '       run bootstrap\install.ps1 first (see docs\INSTALL.md)' -ForegroundColor Gray
    exit 2
}

if (-not (Test-Path -LiteralPath $logs)) { New-Item -ItemType Directory -Path $logs -Force | Out-Null }
$outLog = Join-Path $logs 'bridge.log'
$errLog = Join-Path $logs 'bridge.err.log'

# The bridge reads %WOWHERMES_HOME%\config.toml first.
if (-not $env:WOWHERMES_HOME) { $env:WOWHERMES_HOME = $InstallDir }

function Get-BridgeProcesses {
    $pattern = [regex]::Escape($exe)
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern -or $_.ExecutablePath -eq $exe) })
}

$running = Get-BridgeProcesses
if ($running.Count -gt 0 -and -not $Background) {
    Write-Host "the bridge is already running (pid $($running[0].ProcessId)); stop it with stop.ps1" -ForegroundColor Yellow
    exit 0
}

if ($Background) {
    $launchArgs = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Log', '-InstallDir', "`"$InstallDir`"")
    $proc = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru -ArgumentList $launchArgs
    Write-Host "bridge started in the background (pid $($proc.Id)); log: $outLog"
    exit 0
}

$header = "[{0}] starting wowhermes-bridge (install: {1})" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $InstallDir
Add-Content -LiteralPath $outLog -Value $header

if ($Log) {
    # Blocking, with both streams captured: Task Scheduler sees a running instance.
    $proc = Start-Process -FilePath $exe -ArgumentList 'run' -WorkingDirectory $InstallDir `
        -NoNewWindow -Wait -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    exit $proc.ExitCode
}

& $exe run
exit $LASTEXITCODE
