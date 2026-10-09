param(
    [string]$TaskName = "CondMat Radar Daily Update",
    [string]$At = "06:30",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName"
    exit 0
}

$ScriptPath = Join-Path $PSScriptRoot "update_daily.ps1"
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Incremental OpenAlex/arXiv update, unified FTS rebuild, monitors and legal OA queue" `
    -Force | Out-Null

Write-Host "Installed scheduled task: $TaskName at $At"
Write-Host "The job uses the unified database cursor and single-instance lock."
