#Requires -Version 5.1
<#
.SYNOPSIS
    Stop the WoWHermes bridge (scheduled task and/or process).

.DESCRIPTION
    Stops only the wowhermes-bridge process belonging to this install directory and the
    WoWHermesBridge scheduled task. It never touches Wow.exe or any other game process.

.PARAMETER DryRun
    Report what would be stopped. Alias: -WhatIf.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'wowhermes'),
    [Alias('WhatIf')][switch]$DryRun,
    [string]$TaskName = 'WoWHermesBridge'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$exe = Join-Path $InstallDir 'venv\Scripts\wowhermes-bridge.exe'
$stopped = 0

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and $task.State -eq 'Running') {
    if ($DryRun) {
        Write-Host "would stop scheduled task '$TaskName'"
    } else {
        try {
            Stop-ScheduledTask -TaskName $TaskName
            Write-Host "stopped scheduled task '$TaskName'"
        } catch {
            Write-Host "warning: could not stop task '$TaskName': $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    $stopped++
}

$pattern = [regex]::Escape($exe)
$processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern -or $_.ExecutablePath -eq $exe) })
foreach ($process in $processes) {
    if ($DryRun) {
        Write-Host "would stop process $($process.ProcessId) ($($process.Name))"
    } else {
        try {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
            Write-Host "stopped process $($process.ProcessId) ($($process.Name))"
        } catch {
            Write-Host "warning: could not stop process $($process.ProcessId): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    $stopped++
}

if ($stopped -eq 0) {
    Write-Host 'the bridge is not running'
} elseif ($DryRun) {
    Write-Host 'dry run: nothing was stopped'
}
