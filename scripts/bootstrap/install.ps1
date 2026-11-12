#Requires -Version 5.1
<#
.SYNOPSIS
    Per-user installer for the WoWHermes bridge (Windows companion).

.DESCRIPTION
    Installs the bridge from an extracted wowhermes-bridge-v<version>.zip without Git,
    without a preinstalled Python and without SSH:

      1. finds the WoW Forever client (<...>\_forever_),
      2. resolves uv (PATH, or downloads the pinned official Astral build and verifies SHA256),
      3. installs a managed CPython 3.12 through uv,
      4. `uv sync --frozen --no-dev` in a copy of the bundled bridge source,
      5. writes %LOCALAPPDATA%\wowhermes\config.toml (UTF-8, no BOM),
      6. runs `wowhermes-bridge install` (addon + 256 slot addons) and `status`,
      7. registers an AtLogOn per-user scheduled task (unless -NoAutostart).

    Everything stays under one directory (default %LOCALAPPDATA%\wowhermes) and needs no
    administrator rights. The bridge only makes OUTBOUND HTTP requests to the Hermes
    gateway; this script never opens a firewall port.

    The bearer token is never accepted on the command line by default and is never
    printed. Pass -Token as a SecureString (for automation) or let the script prompt
    with Read-Host -AsSecureString.

.PARAMETER WowDir
    Path to the _forever_ directory (e.g. "D:\Games\World of Warcraft\_forever_").
    When omitted the installer probes the usual install locations and fails with a
    list if it cannot decide.

.PARAMETER GatewayUrl
    Base URL of the Hermes gateway that runs the `wow` adapter, e.g.
    http://192.0.2.10:8791. Required for a first install; an existing config.toml
    keeps its value when the switch is omitted.

.PARAMETER LocalGateway
    Shorthand for http://127.0.0.1:8791 when Hermes runs on this same PC.
    Mutually exclusive with -GatewayUrl.

.PARAMETER Token
    The shared bearer token as a SecureString. Omitting it prompts securely once;
    an existing token in config.toml is preserved unless -ForceToken is given.

.PARAMETER InstallDir
    Per-user install root (default %LOCALAPPDATA%\wowhermes). Useful for a sandbox
    test; all downloads, the venv, logs and config.toml live here.

.PARAMETER NoAutostart
    Do not register the AtLogOn scheduled task. Start the bridge manually with
    <InstallDir>\bin\run.ps1.

.PARAMETER DryRun
    Print the plan and change nothing. Alias: -WhatIf. Combine with -ProvisionTools
    to verify the uv/Python/venv bootstrap in InstallDir while still skipping the
    config, the addon install and the scheduled task.

.PARAMETER ProvisionTools
    With -DryRun: perform the uv download, `uv python install` and
    `uv sync --frozen --no-dev` steps, then stop.

.PARAMETER ForceToken
    Replace an existing token in config.toml (prompts unless -Token is supplied).

.PARAMETER StartNow
    Start the bridge in the background after a successful install.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\bootstrap\install.ps1 `
        -WowDir "D:\Games\World of Warcraft\_forever_" `
        -GatewayUrl "http://192.0.2.10:8791"

.NOTES
    No administrator rights, no Git, no system Python, no SSH. Windows 11 / PowerShell 5.1+.
#>
[CmdletBinding()]
param(
    [string]$WowDir,
    [string]$GatewayUrl,
    [switch]$LocalGateway,
    [SecureString]$Token,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'wowhermes'),
    [switch]$NoAutostart,
    [Alias('WhatIf')][switch]$DryRun,
    [switch]$ProvisionTools,
    [switch]$ForceToken,
    [switch]$StartNow,
    [int]$Slots = 256
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# --- pinned uv (official Astral release) ------------------------------------
$script:UvVersion = '0.13.7'
$script:UvBase = "https://github.com/astral-sh/uv/releases/download/$($script:UvVersion)"
$script:UvSha256 = @{
    'AMD64' = '0cdc5b38fe5a8168ed6a25814f5ba43c21d313dd3187314fccb37b7cec46e021'
    'ARM64' = '505d9e65e7692bac3ddf98c66754aa26c5050c409c184af30a881a0a72cc85e3'
    'x86'   = $null   # 32-bit Windows is not supported by this installer
}
$script:TaskName = 'WoWHermesBridge'
$script:GatewayDefaults = @{ port = 8791 }

# --- output helpers ---------------------------------------------------------
function Write-Step([string]$Message) { Write-Host "[wowhermes] $Message" -ForegroundColor Cyan }
function Write-Detail([string]$Message) { Write-Host "            $Message" -ForegroundColor Gray }
function Write-Note([string]$Message) { Write-Host "[wowhermes] $Message" -ForegroundColor DarkGray }
function Write-Warn([string]$Message) { Write-Host "[wowhermes] WARNING: $Message" -ForegroundColor Yellow }
function Fail([string]$Message, [int]$Code = 1) {
    Write-Host "[wowhermes] ERROR: $Message" -ForegroundColor Red
    exit $Code
}

function Test-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --- url / toml helpers -----------------------------------------------------
function Normalize-GatewayUrl([string]$Url) {
    $value = $Url.Trim()
    if (-not $value) { throw 'empty gateway URL' }
    if ($value -notmatch '^https?://[^\s/]+') {
        throw "gateway URL must start with http:// or https:// (got '$value')"
    }
    $value = $value.TrimEnd('/')
    # Accept a pasted endpoint instead of a base URL; the bridge wants the base.
    $value = $value -replace '/v1/wow/(message|outbox|health)$', ''
    $value = $value -replace '/v1/wow$', ''
    return $value.TrimEnd('/')
}

function ConvertTo-TomlString([string]$Value) {
    $escaped = $Value.Replace('\', '\\').Replace('"', '\"')
    return '"' + $escaped + '"'
}

function Read-ConfigMap([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $map }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed -match '^([A-Za-z0-9_]+)\s*=\s*(.+)$') {
            $map[$Matches[1]] = $Matches[2].Trim()
        }
    }
    return $map
}

function Get-ConfigString([hashtable]$Map, [string]$Key) {
    if (-not $Map.ContainsKey($Key)) { return '' }
    $raw = [string]$Map[$Key]
    if ($raw.Length -ge 2 -and $raw.StartsWith('"') -and $raw.EndsWith('"')) {
        $inner = $raw.Substring(1, $raw.Length - 2)
        return $inner.Replace('\"', '"').Replace('\\', '\')
    }
    return $raw
}

function Write-ConfigFile([string]$Path, [hashtable]$Values, [string[]]$Order) {
    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('# wowhermes-bridge configuration (written by install.ps1)')
    foreach ($key in $Order) {
        if (-not $Values.ContainsKey($key)) { continue }
        $value = $Values[$key]
        if ($value -is [bool]) {
            $literal = if ($value) { 'true' } else { 'false' }
        } elseif ($value -is [int] -or $value -is [long]) {
            $literal = [string]$value
        } else {
            $literal = ConvertTo-TomlString ([string]$value)
        }
        $lines.Add("$key = $literal")
    }
    foreach ($key in $Values.Keys) {
        if ($Order -contains $key) { continue }
        $value = $Values[$key]
        if ($value -is [bool]) {
            $literal = if ($value) { 'true' } else { 'false' }
        } elseif ($value -is [int] -or $value -is [long]) {
            $literal = [string]$value
        } else {
            $literal = ConvertTo-TomlString ([string]$value)
        }
        $lines.Add("$key = $literal")
    }
    $text = ($lines -join "`r`n") + "`r`n"
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = "$Path.tmp"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmp, $text, $encoding)
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

# --- WoW discovery (no user-specific paths) ---------------------------------
function Get-WowCandidates {
    $candidates = New-Object System.Collections.Generic.List[string]
    $add = {
        param([string]$Path)
        if (-not $Path) { return }
        if ($Path -match '\$|%') { return }
        if (Test-Path -LiteralPath $Path -PathType Container) {
            if (-not $candidates.Contains($Path)) { $candidates.Add($Path) }
        }
    }

    if ($env:WOWHERMES_WOW_DIR) { & $add $env:WOWHERMES_WOW_DIR }

    $labels = @('World of Warcraft', 'World of Warcraft Public Test', 'Games\World of Warcraft')
    $drives = @()
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
        if ($drive.Name -notmatch '^[A-Z]$') { continue }
        $drives += "$($drive.Name):\"
    }
    foreach ($root in $drives) {
        foreach ($label in $labels) {
            & $add (Join-Path $root "$label\_forever_")
        }
        foreach ($pf in @('Program Files', 'Program Files (x86)')) {
            & $add (Join-Path $root "$pf\World of Warcraft\_forever_")
        }
        # A second install next door to the standard layout.
        $parent = Join-Path $root 'World of Warcraft'
        if (Test-Path -LiteralPath $parent -PathType Container) {
            foreach ($child in (Get-ChildItem -LiteralPath $parent -Directory -ErrorAction SilentlyContinue)) {
                if ($child.Name -like '_*') { & $add $child.FullName }
            }
        }
    }

    # Registry entries written by Battle.net / the game installer.
    $uninstallRoots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    foreach ($pattern in $uninstallRoots) {
        foreach ($entry in (Get-ItemProperty -Path $pattern -ErrorAction SilentlyContinue)) {
            # StrictMode: a missing registry value is a missing property, not $null.
            $displayName = $entry.PSObject.Properties['DisplayName']
            if (-not $displayName) { continue }
            if ($displayName.Value -notmatch 'World of Warcraft') { continue }
            $location = $entry.PSObject.Properties['InstallLocation']
            if ($location -and $location.Value) { & $add (Join-Path $location.Value '_forever_') }
        }
    }

    return @($candidates)
}

function Resolve-WowDir([string]$Explicit) {
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit -PathType Container)) {
            Fail "WoW directory not found: $Explicit" 2
        }
        if ((Split-Path -Leaf $Explicit.TrimEnd('\')) -ne '_forever_') {
            Write-Warn "this is not a _forever_ folder; it will be used as given (expected ...\_forever_)"
        }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $found = @(Get-WowCandidates)
    if ($found.Count -eq 0) {
        Fail @"
no World of Warcraft: Forever install found.
Pass it explicitly, for example:
  -WowDir "D:\Games\World of Warcraft\_forever_"
A _forever_ folder contains WTF\ and Interface\AddOns\.
"@ 2
    }
    if ($found.Count -gt 1) {
        $list = ($found | ForEach-Object { "  $_" }) -join "`r`n"
        Fail @"
more than one WoW install was found; pick one with -WowDir:
$list
"@ 2
    }
    return $found[0]
}

function Get-AccountName([string]$Wow) {
    $accountRoot = Join-Path $Wow 'WTF\Account'
    if (-not (Test-Path -LiteralPath $accountRoot -PathType Container)) { return '' }
    $dirs = @(Get-ChildItem -LiteralPath $accountRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'SavedVariables' } |
        Sort-Object LastWriteTime -Descending)
    if ($dirs.Count -eq 0) { return '' }
    return $dirs[0].Name
}

# --- toolchain --------------------------------------------------------------
function Test-UvUsable([string]$Version) {
    # `uv python install --no-bin` needs a recent uv. Older PATH builds are ignored
    # and the pinned release is downloaded instead.
    if ($Version -match 'uv (\d+)\.(\d+)\.(\d+)') {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        return ($major -gt 0) -or ($minor -ge 6)
    }
    return $false
}

function Resolve-Uv([string]$ToolsDir) {
    $onPath = Get-Command uv -ErrorAction SilentlyContinue
    if ($onPath) {
        $source = $onPath.Source
        try { $version = (& $source --version) -join '' } catch { $version = '' }
        if (Test-UvUsable $version) {
            Write-Step "uv found on PATH: $source $version"
            return $source
        }
        Write-Note "uv on PATH is too old ($version); using the pinned $($script:UvVersion) release"
    }

    $arch = $env:PROCESSOR_ARCHITECTURE
    if (-not $arch) { $arch = 'AMD64' }
    if ($script:UvSha256[$arch] -eq $null) {
        Fail "unsupported processor architecture '$arch' (need AMD64 or ARM64); install uv yourself from https://docs.astral.sh/uv/ and re-run" 3
    }
    $asset = "uv-$($arch.ToLower().Replace('amd64','x86_64').Replace('arm64','aarch64'))-pc-windows-msvc.zip"
    $url = "$($script:UvBase)/$asset"
    $expected = $script:UvSha256[$arch]

    $downloadDir = Join-Path $ToolsDir 'downloads'
    if (-not (Test-Path -LiteralPath $downloadDir)) { New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null }
    $zipPath = Join-Path $downloadDir $asset

    # uv.exe beside the install root: reuse if already verified.
    $uvExe = Join-Path $ToolsDir 'uv.exe'
    if (Test-Path -LiteralPath $uvExe -PathType Leaf) {
        $current = (Get-FileHash -LiteralPath $uvExe -Algorithm SHA256).Hash.ToLower()
        if ($current -eq $expected) {
            Write-Step "uv already installed: $uvExe"
            return $uvExe
        }
        Write-Note 'cached uv does not match the pinned hash; reinstalling'
        Remove-Item -LiteralPath $uvExe -Force
    }

    Write-Step "downloading uv $($script:UvVersion) ($arch)"
    Write-Detail $url
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    } catch {
        Fail "could not download uv from $url -- $($_.Exception.Message). Install uv manually (https://docs.astral.sh/uv/) and re-run." 3
    }
    $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Remove-Item -LiteralPath $zipPath -Force
        Fail "uv download failed SHA256 verification (expected $expected, got $actual); nothing was installed" 3
    }
    Write-Detail 'SHA256 verified'

    $extract = Join-Path $ToolsDir 'uv-extract'
    if (Test-Path -LiteralPath $extract) { Remove-Item -LiteralPath $extract -Recurse -Force }
    New-Item -ItemType Directory -Path $extract -Force | Out-Null
    try {
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extract -Force
    } catch {
        Fail "could not extract uv: $($_.Exception.Message)" 3
    }
    $found = @(Get-ChildItem -LiteralPath $extract -Recurse -File -Filter 'uv.exe')
    if ($found.Count -ne 1) { Fail "uv archive did not contain exactly one uv.exe (found $($found.Count))" 3 }
    $rootFull = (Resolve-Path -LiteralPath $extract).Path
    if (-not $found[0].FullName.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        Fail 'uv archive tried to write outside the extraction directory' 3
    }
    Move-Item -LiteralPath $found[0].FullName -Destination $uvExe -Force
    Remove-Item -LiteralPath $extract -Recurse -Force
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    Write-Step "uv installed: $uvExe"
    return $uvExe
}

function Copy-BridgeSource([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $items = @('pyproject.toml', 'uv.lock', '.python-version', 'README.md', 'NOTICE', 'wowhermes_bridge')
    foreach ($item in $items) {
        $from = Join-Path $Source $item
        if (-not (Test-Path -LiteralPath $from)) { Fail "release zip is incomplete: missing bridge\$item" 2 }
        Copy-Item -LiteralPath $from -Destination $Destination -Recurse -Force
    }
}

function Invoke-Checked([string]$FilePath, [string[]]$Arguments, [string]$What) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit $LASTEXITCODE)" 4 }
}

# --- main -------------------------------------------------------------------
Write-Host ''
Write-Host 'WoWHermes bridge installer' -ForegroundColor White
Write-Host '--------------------------' -ForegroundColor DarkGray

if ($PSVersionTable.PSVersion.Major -lt 5) { Fail 'PowerShell 5.1 or newer is required.' 2 }
if ($PSVersionTable.PSVersion.Major -eq 5 -and $PSVersionTable.PSVersion.Minor -lt 1) {
    Fail 'PowerShell 5.1 or newer is required.' 2
}

$policy = Get-ExecutionPolicy -Scope CurrentUser
if ($policy -in @('Restricted', 'AllSigned')) {
    Write-Warn @"
the current PowerShell execution policy ($policy) blocks this script.
Re-run it with a one-time bypass (this does not change the machine policy):
  powershell -ExecutionPolicy Bypass -File "$PSCommandPath" ...
"@
}

$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $root 'bridge\pyproject.toml'))) {
    Fail "this script must run from the extracted release zip (expected bridge\pyproject.toml next to bootstrap\). Current root: $root" 2
}
$addonSource = Join-Path $root 'addon\WoWHermes'
if (-not (Test-Path -LiteralPath (Join-Path $addonSource 'WoWHermes.toc'))) {
    Fail "release zip is incomplete: missing addon\WoWHermes\WoWHermes.toc" 2
}

$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$configPath = Join-Path $InstallDir 'config.toml'
$existing = Read-ConfigMap $configPath
$existingToken = Get-ConfigString $existing 'token'
$existingGateway = Get-ConfigString $existing 'gateway_url'
$existingWow = Get-ConfigString $existing 'wow_dir'

$wow = Resolve-WowDir $WowDir
$account = Get-AccountName $wow
Write-Step "WoW client: $wow"
if ($account) { Write-Detail "account (auto-detected): $account" }
else { Write-Detail 'account: not detected (the bridge learns it from the game)' }

# gateway URL: explicit, loopback shorthand, or the existing config
if ($GatewayUrl -and $LocalGateway) { Fail 'use either -GatewayUrl or -LocalGateway, not both' 2 }
if ($LocalGateway) {
    $gateway = "http://127.0.0.1:$($script:GatewayDefaults.port)"
} elseif ($GatewayUrl) {
    try { $gateway = Normalize-GatewayUrl $GatewayUrl } catch { Fail $_.Exception.Message 2 }
} elseif ($existingGateway) {
    $gateway = $existingGateway
    Write-Step "gateway URL from existing config: $gateway"
} else {
    Fail @"
-GatewayUrl is required for a first install. Use the address of the PC that runs Hermes:
  -GatewayUrl "http://192.0.2.10:8791"     # Hermes on another PC in your LAN
  -LocalGateway                              # Hermes on this same PC (http://127.0.0.1:8791)
Find the value in Hermes: Capabilities -> Plugins -> World of Warcraft (the page shows
the gateway URL and a one-time token). Never paste the token on a command line.
"@ 2
}
if ($gateway -notmatch '^https?://') { Fail "invalid gateway URL: $gateway" 2 }
try { $null = [Uri]$gateway } catch { Fail "invalid gateway URL: $gateway" 2 }
if ($gateway -match '^http://(0\.0\.0\.0|\[::\])') {
    Fail "gateway URL '$gateway' is a bind address, not an address the game PC can reach; use the LAN IP or 127.0.0.1" 2
}

# token: explicit SecureString, prompt, or the existing value
$plainToken = ''
$tokenAction = 'keep existing'
if ($Token) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Token)
    try { $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $tokenAction = 'replace'
} elseif ($existingToken -and -not $ForceToken) {
    $plainToken = $existingToken
    $tokenAction = 'keep existing'
} else {
    if ($DryRun -and -not $ProvisionTools) {
        $tokenAction = 'prompt (skipped in dry run)'
    } else {
        Write-Step 'paste the bridge token (input is hidden)'
        $secure = Read-Host -AsSecureString 'Bridge token'
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $tokenAction = 'set'
    }
}
if ($plainToken) {
    if ($plainToken.Length -lt 16) { Write-Warn 'the token is shorter than 16 characters; use the 64-character token Hermes generated' }
    if ($plainToken -match '\s') { Fail 'the token contains whitespace; copy it again without spaces' 2 }
}

$bridgeDir = Join-Path $InstallDir 'bridge'
$venvDir = Join-Path $InstallDir 'venv'
$venvExe = Join-Path $venvDir 'Scripts\wowhermes-bridge.exe'
$toolsDir = Join-Path $InstallDir 'tools'
$logsDir = Join-Path $InstallDir 'logs'
$binDir = Join-Path $InstallDir 'bin'

Write-Step "install root: $InstallDir"
Write-Detail "config:     $configPath"
Write-Detail "gateway:    $gateway"
Write-Detail "token:      $tokenAction (never printed)"

if ($DryRun -and -not $ProvisionTools) {
    Write-Host ''
    Write-Host 'Dry run -- nothing was changed. Planned actions:' -ForegroundColor White
    Write-Host "  1. uv:       PATH, else pinned $($script:UvVersion) -> $toolsDir\uv.exe (SHA256 verified)"
    Write-Host "  2. python:   managed CPython 3.12 under $toolsDir\pythons"
    Write-Host "  3. venv:     $venvDir (uv sync --frozen --no-dev from $bridgeDir)"
    Write-Host "  4. config:   write $configPath (UTF-8 without BOM, token $tokenAction)"
    Write-Host "  5. addon:    wowhermes-bridge install --wow `"$wow`" --addon-src `"$addonSource`" --slots $Slots"
    Write-Host "  6. verify:   wowhermes-bridge status"
    if ($NoAutostart) { Write-Host '  7. task:     skipped (-NoAutostart)' }
    else { Write-Host "  7. task:     register '$($script:TaskName)' at logon (interactive, unlimited, restart 3x)" }
    Write-Host ''
    Write-Host 'Re-run without -DryRun to apply. Add -ProvisionTools to this dry run to verify'
    Write-Host 'the uv/Python/venv bootstrap inside the install root.'
    exit 0
}

$isAdmin = Test-Administrator
if (-not $isAdmin) { Write-Note 'running as a normal user (no administrator rights needed)' }

if (-not (Test-Path -LiteralPath $InstallDir)) {
    try { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null }
    catch { Fail "cannot create $InstallDir -- $($_.Exception.Message). Pick another -InstallDir." 5 }
}
foreach ($dir in @($toolsDir, $logsDir, $binDir)) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

$uv = Resolve-Uv $toolsDir

# Keep every uv artifact under InstallDir so uninstall is complete.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $toolsDir 'pythons'
$env:UV_CACHE_DIR = Join-Path $toolsDir 'cache'
$env:UV_PROJECT_ENVIRONMENT = $venvDir
$env:UV_PYTHON_PREFERENCE = 'only-managed'
if (-not (Test-Path -LiteralPath $env:UV_PYTHON_INSTALL_DIR)) { New-Item -ItemType Directory -Path $env:UV_PYTHON_INSTALL_DIR -Force | Out-Null }

Write-Step 'installing managed CPython 3.12 (uv python install 3.12)'
# --no-bin: no shims in ~\.local\bin -- everything stays under InstallDir.
& $uv python install 3.12 --no-bin
if ($LASTEXITCODE -ne 0) { Fail 'uv could not install Python 3.12; check your internet connection and re-run' 4 }

Write-Step 'staging the bridge source'
Copy-BridgeSource (Join-Path $root 'bridge') $bridgeDir

Write-Step 'resolving dependencies (uv sync --frozen --no-dev)'
& $uv sync --frozen --no-dev --directory $bridgeDir
if ($LASTEXITCODE -ne 0) { Fail 'uv sync failed; see the output above (proxy/TLS issues are the usual cause)' 4 }
if (-not (Test-Path -LiteralPath $venvExe)) { Fail "venv is incomplete: $venvExe is missing after uv sync" 4 }
$bridgeVersion = (& $venvExe --version) -join ''
Write-Step "bridge installed: $bridgeVersion"

if ($DryRun) {
    Write-Host ''
    Write-Host 'Toolchain provisioning verified (dry run): config, addon install and the scheduled task were skipped.' -ForegroundColor Green
    Write-Host "  bridge exe: $venvExe"
    Write-Host "  python:     $env:UV_PYTHON_INSTALL_DIR"
    exit 0
}

# --- config.toml (UTF-8, no BOM, token never logged) ------------------------
$values = @{
    wow_dir     = $wow
    account     = $account
    gateway_url = $gateway
    token       = $plainToken
    character   = (Get-ConfigString $existing 'character')
    transport   = 'strip'
    overlay     = $false
    slot_count  = $Slots
}
foreach ($key in @('character', 'transport')) {
    if ($existing.ContainsKey($key)) { $values[$key] = Get-ConfigString $existing $key }
}
if ($existing.ContainsKey('overlay')) {
    $values['overlay'] = ($existing['overlay'] -eq 'true')
}
$order = @('wow_dir', 'account', 'gateway_url', 'token', 'character', 'transport', 'overlay', 'slot_count')
try {
    Write-ConfigFile $configPath $values $order
} catch {
    Fail "could not write $configPath -- $($_.Exception.Message)" 5
}
Write-Step "config written: $configPath (token: $tokenAction, not printed)"

# --- addon + slot install ---------------------------------------------------
Write-Step 'installing the addon and slot addons'
$env:WOWHERMES_HOME = $InstallDir
& $venvExe install --wow $wow --addon-src $addonSource --slots $Slots
if ($LASTEXITCODE -ne 0) { Fail 'the addon install failed -- close World of Warcraft and re-run, or check write access to the AddOns folder' 5 }
Write-Step 'status'
& $venvExe status
if ($LASTEXITCODE -ne 0) { Fail 'wowhermes-bridge status failed' 5 }

# --- gateway reachability (informational; the bridge retries in its loop) ----
$healthUrl = "$gateway/v1/wow/health"
try {
    $probe = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 8 `
        -Headers @{ Authorization = "Bearer $plainToken" }
    Write-Step "gateway reachable: $healthUrl (HTTP $([int]$probe.StatusCode))"
} catch {
    Write-Warn "could not reach $healthUrl -- $($_.Exception.Message)"
    Write-Detail 'the install is complete and the bridge retries; check Hermes is running, the URL, and the Hermes-host firewall rule.'
}

# --- helper scripts ---------------------------------------------------------
$bootstrapDir = $PSScriptRoot
foreach ($name in @('run.ps1', 'stop.ps1', 'uninstall.ps1')) {
    $source = Join-Path $bootstrapDir $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $binDir $name) -Force
    }
}

# --- scheduled task ---------------------------------------------------------
if ($NoAutostart) {
    Write-Step 'autostart skipped (-NoAutostart)'
    Write-Detail "start the bridge with: $binDir\run.ps1"
} else {
    Write-Step "registering the per-user AtLogOn task '$($script:TaskName)'"
    try {
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument ('-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Log' -f (Join-Path $binDir 'run.ps1')) `
            -WorkingDirectory $binDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $script:TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force `
            -Description 'WoWHermes bridge: reads the in-game strip, talks to the Hermes gateway. Runs at logon.' | Out-Null
        Write-Detail "task registered: $($script:TaskName) (runs at logon, unlimited duration, restarts 3x)"
    } catch {
        Write-Warn @"
could not register the scheduled task: $($_.Exception.Message)
The install is still usable. Start it manually with:
  $binDir\run.ps1
or create the task yourself (per-user, no admin):
  schtasks /create /tn $($script:TaskName) /tr "powershell -WindowStyle Hidden -File \"$binDir\run.ps1\" -Log" /sc onlogon /rl limited /f
"@
    }
}

if ($StartNow) {
    Write-Step 'starting the bridge in the background'
    try {
        Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden `
            -ArgumentList ('-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -Log' -f (Join-Path $binDir 'run.ps1')) | Out-Null
    } catch {
        Write-Warn "could not start the bridge: $($_.Exception.Message)"
    }
}

# --- final notes ------------------------------------------------------------
Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host @"
Next steps
  1. In WoW: Options -> Graphics -> Display Mode = "Windowed (Fullscreen)".
     Exclusive fullscreen cannot be captured.
  2. Keep the top-left corner of the game window clear: the addon draws its status
     strip in the top-left 1600x400 pixels. Move or hide any action bar / minimap
     overlay that covers it.
  3. Type /reload in game (or restart WoW) so the addon and its 256 slot addons load.
  4. Logs: $logsDir\bridge.log   (start/stop: $binDir\run.ps1 / stop.ps1)

Network: the bridge only makes OUTBOUND HTTP requests to $gateway.
No firewall port is opened on this PC; if Windows prompts you, block inbound and
allow outbound. If Hermes runs on this same PC, use -LocalGateway instead.
"@
exit 0
