#Requires -Version 5.1
<#
.SYNOPSIS
  One-line installer for ninfs-mcp: code + venv + client auto-registration.
.EXAMPLE
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Fizor-drm/ninfs-mcp/main/tools/install.ps1))) `
    -Workspace C:\analysis -Movable G:\keys\movable.sed -Boot9 G:\keys\boot9.bin
#>
[CmdletBinding()]
param(
    [Parameter()] [string] $Workspace = "",
    [Parameter()] [string] $Movable = "",
    [Parameter()] [string] $Boot9 = "",
    [Parameter()] [string] $SdRoot = "",
    [Parameter()] [string] $InstallDir = (Join-Path $env:LOCALAPPDATA "ninfs-mcp"),
    [Parameter()] [string[]] $Clients = @("auto"),
    [Parameter()] [switch] $CheckOnly
)

$ErrorActionPreference = "Stop"
$Repo = "https://github.com/Fizor-drm/ninfs-mcp.git"
$ZipUrl = "https://github.com/Fizor-drm/ninfs-mcp/archive/refs/heads/main.zip"

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
if (-not [string]::IsNullOrWhiteSpace($Workspace)) {
    $Workspace = [IO.Path]::GetFullPath($Workspace)
}

function Test-Prereqs {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { throw "python not found on PATH (need 3.10+)" }
    $ver = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
    if ([version]$ver -lt [version]"3.10") { throw "python $ver < 3.10" }
    if (-not (Get-Service WinFsp.Launcher -ErrorAction SilentlyContinue)) {
        Write-Warning "WinFsp.Launcher service not found; mounts will fail until WinFsp 2.x is installed."
    }
    if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
        Write-Warning "cl.exe not found; haccrypto build needs MSVC C++ tools."
    }
    Write-Host "prereqs ok (python $ver)"
}

function Install-Code {
    if (Test-Path (Join-Path $InstallDir ".git")) {
        git -C $InstallDir pull --ff-only | Out-Null
        return
    }
    if ((Get-Command git -ErrorAction SilentlyContinue) -and -not (Test-Path $InstallDir)) {
        git clone --depth 1 $Repo $InstallDir | Out-Null
        return
    }
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("ninfs-mcp-" + [Guid]::NewGuid().ToString("N") + ".zip")
    Invoke-WebRequest -Uri $ZipUrl -OutFile $tmp
    $stage = Join-Path ([IO.Path]::GetTempPath()) ("ninfs-mcp-" + [Guid]::NewGuid().ToString("N"))
    Expand-Archive -Path $tmp -DestinationPath $stage | Out-Null
    $src = Join-Path $stage "ninfs-mcp-main"
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Move-Item $src $InstallDir | Out-Null
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue | Out-Null
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue | Out-Null
}

function Install-Deps {
    $venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        & python -m venv (Join-Path $InstallDir ".venv") | Out-Null
    }
    & $venvPy -m pip install -q $InstallDir 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "pip install failed (haccrypto needs MSVC C++ tools). Install 'Visual Studio Build Tools - C++ build tools', then re-run this script."
    }
    & $venvPy -m ninfs_mcp.server --help 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "server smoke test failed." }
    return $venvPy
}

function Read-JsonTolerant([string] $Path) {
    $text = [IO.File]::ReadAllText($Path)
    $text = [regex]::Replace($text, '(?m)^\s*//.*$', '')
    $text = [regex]::Replace($text, '/\*.*?\*/', '', 'Singleline')
    return $text | ConvertFrom-Json -AsHashtable
}

function Write-JsonBackup([string] $Path, [hashtable] $Data) {
    if (Test-Path $Path) {
        Copy-Item $Path "$Path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" -Force
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path $Path) | Out-Null
    }
    $Data | ConvertTo-Json -Depth 10 | Set-Content $Path -Encoding UTF8
}

function Register-Clients([string] $VenvPy) {
    $entry = @{
        command = $VenvPy
        args    = @("-m", "ninfs_mcp.server")
        env     = @{
            NINFS_SD_ROOT      = $SdRoot
            NINFS_MOVABLE_PATH = $Movable
            NINFS_BOOT9_PATH   = $Boot9
            NINFS_WORKSPACE    = $Workspace
        }
    }
    $want = $Clients
    if ($want -contains "auto") {
        $want = @("claude-code", "opencode", "cursor", "windsurf")
    }
    foreach ($client in $want) {
        switch ($client) {
            "claude-code" {
                $p = Join-Path $env:USERPROFILE ".claude.json"
                $d = @{}
                if (Test-Path $p) { $d = Read-JsonTolerant $p }
                if (-not $d["mcpServers"]) { $d["mcpServers"] = @{} }
                $d["mcpServers"]["ninfs-mcp"] = $entry
                Write-JsonBackup $p $d
                Write-Host "registered: Claude Code ($p)"
            }
            "opencode" {
                $p = Join-Path $env:USERPROFILE ".config\opencode\opencode.jsonc"
                $d = @{}
                if (Test-Path $p) { $d = Read-JsonTolerant $p }
                if (-not $d["mcp"]) { $d["mcp"] = @{} }
                $d["mcp"]["ninfs-mcp"] = @{
                    type    = "local"
                    command = @($VenvPy, "-m", "ninfs_mcp.server")
                    env     = $entry.env
                    enabled = $true
                }
                Write-JsonBackup $p $d
                Write-Host "registered: OpenCode ($p)"
            }
            "cursor" {
                $p = Join-Path $env:USERPROFILE ".cursor\mcp.json"
                $d = @{}
                if (Test-Path $p) { $d = Read-JsonTolerant $p }
                if (-not $d["mcpServers"]) { $d["mcpServers"] = @{} }
                $d["mcpServers"]["ninfs-mcp"] = $entry
                Write-JsonBackup $p $d
                Write-Host "registered: Cursor ($p)"
            }
            "windsurf" {
                $p = Join-Path $env:USERPROFILE ".codeium\windsurf\mcp_config.json"
                $d = @{}
                if (Test-Path $p) { $d = Read-JsonTolerant $p }
                if (-not $d["mcpServers"]) { $d["mcpServers"] = @{} }
                $d["mcpServers"]["ninfs-mcp"] = $entry
                Write-JsonBackup $p $d
                Write-Host "registered: Windsurf ($p)"
            }
            default { Write-Warning "unknown client: $client (skipped)" }
        }
    }
}

Test-Prereqs
if ($CheckOnly) {
    Write-Host "check only: no changes made."
    return
}
if ([string]::IsNullOrWhiteSpace($Workspace)) { throw "-Workspace is required" }
if ([string]::IsNullOrWhiteSpace($Movable)) { throw "-Movable is required" }
if ([string]::IsNullOrWhiteSpace($Boot9)) { throw "-Boot9 is required" }
Install-Code
$venvPy = Install-Deps
Register-Clients $venvPy
Write-Host ""
Write-Host "done. Restart your AI client to load ninfs-mcp."
