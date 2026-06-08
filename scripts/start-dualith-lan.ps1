$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root ".tmp"
$LogPath = Join-Path $LogDir "dualith-startup.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $Root

"[$(Get-Date -Format o)] Starting Dualith LAN dev server from $Root" | Add-Content -Path $LogPath

$Npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $Npm) {
  $Npm = Get-Command npm -ErrorAction Stop
}

& $Npm.Source run dev:lan *>> $LogPath
$ExitCode = $LASTEXITCODE

"[$(Get-Date -Format o)] Dualith LAN dev server exited with code $ExitCode" | Add-Content -Path $LogPath
exit $ExitCode
