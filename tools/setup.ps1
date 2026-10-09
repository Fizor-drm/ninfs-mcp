#Requires -Version 5.1
<#
.SYNOPSIS
  Bootstrap ninfs-mcp: create venv, install deps, verify the server.
.NOTES
  Requires: Windows, Python 3.10+, WinFsp (for actual mounts),
  a C compiler (for haccrypto), SD backup + movable.sed + boot9.bin.
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python not found on PATH (need 3.10+)"
}
if (-not (Test-Path (Join-Path $Root ".venv"))) {
    python -m venv (Join-Path $Root ".venv")
}
$Py = Join-Path $Root ".venv\Scripts\python.exe"
& $Py -m pip install -e $Root
& $Py -m ninfs_mcp.server --help | Out-Null

Write-Host ""
Write-Host "ninfs-mcp ready. Next:"
Write-Host "  1. Set env: NINFS_MOVABLE_PATH, NINFS_BOOT9_PATH, NINFS_WORKSPACE"
Write-Host "  2. Point your MCP client at: $Py -m ninfs_mcp.server"
Write-Host "  3. Or build a one-click bundle: python tools/build_mcpb.py"
