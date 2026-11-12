#Requires -Version 5.1
<#
.SYNOPSIS
    Remove the WoWHermes bridge installed by install.ps1.

.DESCRIPTION
    Removes the scheduled task, stops the bridge, deletes the install root (venv, tools,
    bridge source, logs, helper scripts) and -- only with -RemoveAddon -- deletes the
    addon folder and its 256 slot addons from the game.

    config.toml (which holds the token) is kept by default so a later reinstall keeps
    working; use -Purge to delete it as well. The World of Warcraft client is never
    started or stopped by this script; close it before -RemoveAddon so the game does
    not rewrite the folders on logout.

.PARAMETER RemoveAddon
    Also delete <wow>\Interface\AddOns\WoWHermes and WoWHermes_S### slot addons.
    The WoW directory comes from -WowDir or from config.toml.

.PARAMETER Purge
    Delete config.toml (and with it the saved token).

.PARAMETER DryRun
    Report what would be removed. Alias: -WhatIf.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'wowhermes'),
    [string]$WowDir,
    [switch]$RemoveAddon,
    [switch]$Purge,
    [Alias('WhatIf')][switch]$DryRun,
    [string]$TaskName = 'WoWHermesBridge'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$configPath = Join-Path $InstallDir 'config.toml'

function Get-ConfigWowDir([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return '' }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        if ($line -match '^\s*wow_dir\s*=\s*"(.+)"\s*$') {
            return $Matches[1].Replace('\\', '\').Replace('\"', '"')
        }
    }
    return ''
}

function Remove-PathSafe([string]$Path, [string]$ExpectedRoot) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\') + '\'
    if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "refusing to delete $full (outside $root)"
    }
    if (Test-Path -LiteralPath $full) {
        if ($DryRun) { Write-Host "would delete $full" }
        else { Remove-Item -LiteralPath $full -Recurse -Force; Write-Host "deleted $full" }
    }
}

Write-Host 'WoWHermes bridge uninstall' -ForegroundColor White
if ($DryRun) { Write-Host '(dry run -- nothing will be changed)' -ForegroundColor Gray }

# 1. stop bridge
$stopScript = Join-Path $InstallDir 'bin\stop.ps1'
if (Test-Path -LiteralPath $stopScript) {
    & $stopScript -InstallDir $InstallDir -TaskName $TaskName -DryRun:$DryRun
} else {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running') {
        if ($DryRun) { Write-Host "would stop scheduled task '$TaskName'" }
        else { Stop-ScheduledTask -TaskName $TaskName }
    }
}

# 2. scheduled task
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($DryRun) {
        Write-Host "would unregister scheduled task '$TaskName'"
    } else {
        try {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "removed scheduled task '$TaskName'"
        } catch {
            Write-Host "warning: could not remove task '$TaskName': $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
}

# 3. addon folders (opt-in)
if ($RemoveAddon) {
    $wow = $WowDir
    if (-not $wow) { $wow = Get-ConfigWowDir $configPath }
    if (-not $wow -or -not (Test-Path -LiteralPath $wow -PathType Container)) {
        Write-Host 'warning: no WoW directory known; skipping addon removal (pass -WowDir)' -ForegroundColor Yellow
    } else {
        $addonsDir = Join-Path $wow 'Interface\AddOns'
        if (Test-Path -LiteralPath $addonsDir -PathType Container) {
            $targets = @(Get-ChildItem -LiteralPath $addonsDir -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq 'WoWHermes' -or $_.Name -match '^WoWHermes_S\d{3}$' })
            foreach ($target in $targets) { Remove-PathSafe $target.FullName $addonsDir }
            if ($targets.Count -eq 0) { Write-Host "no WoWHermes folders under $addonsDir" }
            Write-Host 'start WoW (or /reload) to unload the addon.' -ForegroundColor Gray
        }
    }
} else {
    Write-Host 'addon folders kept; add -RemoveAddon to delete them from the game.' -ForegroundColor Gray
}

# 4. install root
if (Test-Path -LiteralPath $InstallDir) {
    $children = @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        if ($child.Name -eq 'config.toml' -and -not $Purge) { continue }
        Remove-PathSafe $child.FullName $InstallDir
    }
    $left = @(Get-ChildItem -LiteralPath $InstallDir -Force -ErrorAction SilentlyContinue)
    if ($left.Count -eq 0 -and -not $DryRun) {
        Remove-Item -LiteralPath $InstallDir -Force
        Write-Host "deleted $InstallDir"
    } elseif ($left.Count -gt 0) {
        Write-Host "kept: $($left.FullName -join ', ')"
        if (-not $Purge -and ($left.Name -contains 'config.toml')) {
            Write-Host 'config.toml (token) kept; re-run with -Purge to delete it.' -ForegroundColor Gray
        }
    }
}

Write-Host ''
if ($DryRun) { Write-Host 'dry run complete.' -ForegroundColor Green }
else { Write-Host 'uninstall complete.' -ForegroundColor Green }
