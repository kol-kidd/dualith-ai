$ErrorActionPreference = "Stop"

$ShortcutName = "Dualith LAN.lnk"
$StartScript = Join-Path $PSScriptRoot "start-dualith-lan.ps1"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir $ShortcutName

if (-not (Test-Path -LiteralPath $StartScript)) {
  throw "Startup script not found: $StartScript"
}

$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`""
$Shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$Shortcut.Description = "Start Dualith LAN mode at Windows login"
$Shortcut.Save()

Write-Host "Created startup shortcut: $ShortcutPath"
