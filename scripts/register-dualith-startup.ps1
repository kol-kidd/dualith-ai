$ErrorActionPreference = "Stop"

$TaskName = "Dualith LAN"
$StartScript = Join-Path $PSScriptRoot "start-dualith-lan.ps1"

if (-not (Test-Path -LiteralPath $StartScript)) {
  throw "Startup script not found: $StartScript"
}

$Action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Description "Start Dualith LAN mode on login" `
  -Force `
  -ErrorAction Stop | Out-Null

Write-Host "Registered scheduled task: $TaskName"
